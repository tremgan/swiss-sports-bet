# 🇨🇭 Swiss Sports Bet Markets

> **Disclaimer:** This project is built for **educational and portfolio purposes only**. It is not intended for commercial use, real-money betting, or any activity that violates the terms of service of the data sources referenced. The scraping code is provided as a technical demonstration of web scraping, data engineering, and microservice architecture patterns.

A Python microservice application that scrapes real-time football (soccer) betting odds from multiple Swiss bookmakers, matches events across sources using fuzzy string matching, and surfaces cross-bookmaker odds comparisons and arbitrage opportunities through a Streamlit dashboard.

## Project Status

The full pipeline is functional: scraping, storage, cross-bookmaker matching, arbitrage detection and the dashboard. Scrapers run on a configurable interval (`SCRAPE_FREQUENCY_HOURS`). The database schema is managed with Alembic migrations, and CI runs linting, type checking and 46 tests on every push.

## Architecture

Four independent services communicate over HTTP, plus a shared library:

```
+-----------------+   +----------------------+
|  Loro Scraper   |   |  Swisslos Scraper    |
|  (REST API)     |   |  (Playwright/WS)     |
+--------+--------+   +----------+-----------+
         |                       |
         |   POST /bookmaker_    |
         |   matches/ & odds/    |
         v                       v
      +-----------------------------+
      |       DB Service            |
      |  (FastAPI + SQLModel)       |
      |                             |
      |  : stores bookmaker odds    |
      |  : fuzzy-matches events     |
      |    across bookmakers        |
      |  : serves paired odds       |
      +--------------+--------------+
                     |
                     | GET /matches/with_odds/
                     v
              +-------------+
              |  Dashboard   |
              | (Streamlit)  |
              +-------------+
```

### Services

**core** : Shared library. SQLModel data models, the arbitrage engine, the scrape/publish runtime both scrapers share, and one logging setup. Installed as an editable dependency via uv.

**loro_scrape_service** : Scrapes football betting odds from Loterie Romande (Loro), whose sportsbook runs on OpenBet. One `event-list` call returns fixtures and their 1X2 market together; the parser maps outcome `subType` (`H`/`D`/`A`) to odds, which is language-independent unlike the outcome names.

**swisslos_scrape_service** : Scrapes football betting odds from Swisslos by launching a headless Chromium browser via Playwright, intercepting WebSocket frames, and decompressing the binary (zlib-deflate) payloads to extract event and odds data.

**db_service** : FastAPI backend that stores all scraped data in a SQL database (SQLite for dev, PostgreSQL for production). Includes a match-making engine (`match_maker.py`) that uses time-window filtering and fuzzy string matching (rapidfuzz `token_sort_ratio`) to reconcile the same football match across different bookmakers. Exposes endpoints for writing odds, triggering matching, and reading paired cross-bookmaker odds.

**dashboard** : Streamlit app that displays matched events with odds from multiple bookmakers side by side, flagging arbitrage opportunities and showing the stake split for each.

## Data Model

```
Match (canonical event)
|-- match_label          "FC Basel vs FC Zurich"
|-- match_datetime       2026-03-25 18:00 UTC
|-- team1, team2
|
+-- BookmakerMatch (one per bookmaker per match)
    |-- bookmaker        "Loro" / "Swisslos"
    |-- match_label      (may differ slightly between bookmakers)
    |-- match_datetime
    |-- matching_attempts
    |
    +-- SportsBettingOdds (one per scrape run)
        |-- team1_odds
        |-- draw_odds
        |-- team2_odds
        +-- timestamp
```

An entity diagram is in [`docs/erd.html`](docs/erd.html).

## Cross-Bookmaker Matching

Loro and Swisslos name teams differently and may report slightly different kick-off times, so a direct join is not possible. The `match_maker` module handles this by:

1. Querying all BookmakerMatch rows not yet linked to a canonical Match (with fewer than 3 matching attempts).
2. For each, searching for existing Match records within a configurable time window (default: +/- 1 hour).
3. Attempting an exact match on label + datetime first.
4. Falling back to fuzzy matching using `rapidfuzz.fuzz.token_set_ratio` with a configurable threshold (default: 85).
5. If no match is found, creating a new canonical Match from the bookmaker data.

Both scrapers trigger the matching process automatically after posting data via `POST /run_matching/`.

`token_set_ratio` is used rather than `token_sort_ratio` because the bookmakers disagree on how much of a club's name to include — Swisslos reports "FC Thun vs Grasshopper Club Zurich" where Loro reports "Thun vs Grasshopper". Comparing on the shared token set scores those 100; sorting tokens scores 72 and would drop the pair entirely.

## Arbitrage Detection

A bookmaker's own book always overrounds: its implied probabilities sum to more than 1, and the excess is its margin. Taking the *best* price for each outcome across several bookmakers can push that sum below 1, at which point a stake split proportional to the implied probabilities returns the same payout whichever way the match goes.

`core.arbitrage.analyse()` takes the `bookmaker_odds` payload from `GET /matches/with_odds/` and returns the best price per outcome, the combined margin, and — when the book is beatable — the stake split and guaranteed profit. Two-way markets (no draw priced) are handled as well as 1X2.

```python
>>> from core.arbitrage import analyse
>>> result = analyse({
...     "Loro":     {"team1_odds": 2.1, "draw_odds": 3.6, "team2_odds": 4.0},
...     "Swisslos": {"team1_odds": 1.8, "draw_odds": 3.5, "team2_odds": 5.0},
... })
>>> result.has_arbitrage, round(result.margin_pct, 2), round(result.profit_pct, 2)
(True, -4.6, 4.83)
>>> result.best_odds["team2"]
('Swisslos', 5.0)
```

## Tech Stack

- Python 3.13, type-checked with pyright
- FastAPI + Uvicorn for the API layer
- SQLModel (SQLAlchemy + Pydantic) for ORM and validation
- Alembic for database migrations
- Playwright for headless browser automation and WebSocket interception
- rapidfuzz for fuzzy string matching across bookmakers
- Streamlit for the dashboard
- uv for dependency management, ruff for linting and formatting
- Docker + Docker Compose for containerization and orchestration
- GitHub Actions for CI/CD

## Project Structure

```
swiss-sports-bet/
|-- Makefile                        # sync / lint / typecheck / test / check
|-- ruff.toml                       # lint + format config for the whole repo
|-- docker-compose.yaml
|-- .env.example                    # copy to .env
|-- .github/workflows/
|   |-- test.yaml                   # lint, type check, test (per service)
|   +-- deploy.yaml                 # calls test.yaml, then deploys to the VPS
|-- docs/
|   +-- erd.html                    # entity relationship diagram
+-- src/
    |-- core/
    |   |-- core/
    |   |   |-- models.py           # shared SQLModel data models
    |   |   |-- arbitrage.py        # arbitrage detection
    |   |   |-- scraper.py          # shared scrape/publish/reconcile runtime
    |   |   +-- logging_config.py   # one logging setup for all services
    |   +-- tests/
    |-- db_service/
    |   |-- main.py                 # FastAPI endpoints
    |   |-- repositories.py         # database access
    |   |-- match_maker.py          # cross-bookmaker fuzzy matching logic
    |   |-- config.py               # DB engine setup from env
    |   |-- migrations/             # Alembic
    |   +-- tests/
    |-- loro_scrape_service/
    |   |-- main.py                 # Loro (OpenBet) REST scraper
    |   +-- tests/                  # parser tests against a recorded fixture
    |-- swisslos_scrape_service/
    |   +-- main.py                 # Swisslos Playwright/WS scraper
    +-- dashboard/
        +-- main.py                 # Streamlit dashboard
```

Each service has its own `pyproject.toml`, `uv.lock` and `Dockerfile`.

## Setup

### Prerequisites

- Python 3.13+
- [uv](https://docs.astral.sh/uv/)
- Chromium (installed by Playwright for the Swisslos scraper)

### Environment Variables

```bash
cp .env.example .env
```

| Variable | Used by | Purpose |
|---|---|---|
| `SQLMODEL_DB_URL` | db_service | Database connection string (`sqlite:///dev.db` locally, PostgreSQL in production) |
| `DB_SERVICE_URL` | scrapers, dashboard | Where to reach the API (default `http://127.0.0.1:8000`) |
| `SCRAPE_FREQUENCY_HOURS` | scrapers | Interval between runs (default `3`) |
| `POSTGRES_USER` / `POSTGRES_PASSWORD` / `POSTGRES_DB` | docker-compose | Postgres credentials |

### Running Locally

1. Start the DB service:

```bash
cd src/db_service
uv sync
export SQLMODEL_DB_URL=sqlite:///dev.db
uv run alembic upgrade head          # create/update the schema
uv run uvicorn main:app --reload
```

Interactive API docs are then at http://127.0.0.1:8000/docs.

2. Run the scrapers:

```bash
# Loro (quick, REST-based)
cd src/loro_scrape_service
uv sync
uv run main.py

# Swisslos (launches headless browser, takes ~90s per run)
cd src/swisslos_scrape_service
uv sync
uv run playwright install chromium --with-deps
uv run main.py
```

3. Launch the dashboard:

```bash
cd src/dashboard
uv sync
uv run streamlit run main.py
```

### Running with Docker Compose

From the project root:

```bash
docker compose up --build
```

This starts PostgreSQL, the DB service (internal port 8000), both scrapers, and the dashboard on http://localhost:8501. The DB service runs `alembic upgrade head` on start, so the schema is always current.

## Development

```bash
make sync        # uv sync every service
make lint        # ruff check + ruff format --check
make typecheck   # pyright, per service against its own venv
make test        # pytest, per service
make check       # all of the above
```

### Database migrations

The schema lives in `core.models` and is applied through Alembic:

```bash
cd src/db_service
uv run alembic revision --autogenerate -m "describe the change"
uv run alembic upgrade head
uv run alembic check                 # fails if models and migrations disagree
```

## CI/CD

`test.yaml` runs ruff over the whole repo, then type-checks and tests each service in a matrix. `deploy.yaml` calls it and, only if it passes, SSHes into the VPS, pulls, and rebuilds the containers.

The workflow requires three repository secrets:

| Secret | Description |
|---|---|
| `VPS_HOST` | Public IP or hostname of the VPS |
| `VPS_USER` | SSH username (e.g. `root` or a deploy user) |
| `VPS_SSH_KEY` | Private SSH key with access to the VPS |

The project is expected to be cloned at `~/projects/sports-bet` on the VPS before the first deploy.

## API Endpoints

| Method | Path | Description |
|---|---|---|
| `GET` | `/` | Health check |
| `POST` | `/bookmaker_matches/` | Create a bookmaker match, or return the existing row on a repeat |
| `GET` | `/bookmaker_matches/` | List bookmaker matches (`limit`, `offset`) |
| `POST` | `/sports_betting_odds/` | Record a single odds snapshot |
| `POST` | `/sports_betting_odds/bulk/` | Record several odds snapshots in one transaction |
| `GET` | `/sports_betting_odds/` | List odds snapshots, newest first (`limit`, `offset`) |
| `POST` | `/run_matching/` | Trigger cross-bookmaker matching |
| `GET` | `/matches/with_odds/` | Matches priced by more than one bookmaker (`limit`, `offset`) |

## Roadmap

- **Real-time alerts** : notify via Telegram or webhook when an arbitrage opportunity is detected
- **Additional bookmakers** : extend coverage beyond Loro and Swisslos
- **Historical odds tracking** : visualize odds movement over time in the dashboard
- **Scraper parser tests** : record WebSocket/API fixtures and cover the parsing paths
