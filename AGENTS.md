# AGENTS.md

Working notes for agents and new contributors. The README is the user-facing
document; this file records how the repo is put together and the things that
have bitten us.

## What this project is

Scrapes football odds from two Swiss bookmakers (Loro and Swisslos), links the
same fixture across both, and publishes a cross-bookmaker comparison with any
arbitrage opportunities as a static page.

It runs entirely on free infrastructure. GitHub Actions executes the pipeline on
a cron, Supabase holds the odds between runs, GitHub Pages serves the output.

## Current state (2026-10-01)

Branch `scheduled-pipeline`, not yet merged to `main`.

Done:

- Supabase Postgres is live and migrated. `alembic upgrade head` has run against
  it; `alembic_version`, `match`, `bookmakermatch` and `sportsbettingodds`
  exist.
- `DATABASE_URL` is set as a repository secret and in local `.env`.
- `scrape.yaml` implements the full pipeline: migrate, serve, scrape both,
  guard, render, deploy to Pages.
- The VPS and Docker deployment has been removed: `docker-compose.yaml`,
  `deploy.yaml`, all four `Dockerfile`s and `.dockerignore` are gone, along with
  the `POSTGRES_*` variables that only compose consumed.
- The Streamlit dashboard (`src/dashboard/`) has been deleted. `src/report/`
  replaces it, rendering the same `GET /matches/with_odds/` payload to a file.

Not done:

- The branch has never run the pipeline end to end on Actions.
- The `VPS_HOST`, `VPS_USER` and `VPS_SSH_KEY` repository secrets still exist
  and are now unused.
- A `Spike - runner scrape` workflow still exists on the remote. It is not in
  the tree on this branch.
- GitHub Pages may still need enabling in repository settings (Source: GitHub
  Actions).

## Layout

Each directory under `src/` is an independent uv project with its own
`pyproject.toml` and `uv.lock`. `core` is a path dependency installed editable
into each of the others. There is no root-level Python project.

| Service | Role |
|---|---|
| `core` | SQLModel models, arbitrage engine, shared scrape runtime, logging setup |
| `db_service` | FastAPI API over the database, plus Alembic migrations |
| `loro_scrape_service` | Loro (OpenBet) REST scraper |
| `swisslos_scrape_service` | Swisslos scraper, Playwright intercepting WebSocket frames |
| `report` | Jinja2 renderer producing `dist/index.html` |

Adding or removing a service means touching four places: `Makefile`
(`SERVICES`), `ruff.toml` (`src`), `.github/workflows/test.yaml` (the matrix),
and this file.

## Commands

```bash
make check       # lint + typecheck + test, matching CI
make sync        # uv sync every service
```

Run anything else from inside the service directory, since each has its own
venv. `make typecheck` loops because pyright needs each service's own venv to
resolve its dependencies.

Tests at last count: 112 across core (48), db_service (42),
loro_scrape_service (14), swisslos_scrape_service (2) and report (6).

## Gotchas

**The Supabase connection string has three constraints at once.** Use the
session pooler (`<region>.pooler.supabase.com`, port 5432) with the
`postgresql+psycopg://` scheme. The direct connection is IPv6-only on the free
tier and unreachable from Actions runners. The transaction pooler on 6543 keeps
no session state, which breaks Alembic and psycopg 3's prepared statements. A
bare `postgresql://` scheme makes SQLAlchemy look for psycopg2, which is not a
dependency. The pooler username carries the project ref:
`postgres.<project-ref>`.

**Percent signs in `DATABASE_URL`.** `migrations/env.py` doubles `%` before
handing the URL to Alembic's ConfigParser, which would otherwise read a
percent-encoded password as an interpolation token. Preserve that if you touch
the file.

**Workflows only trigger from the default branch.** Both `schedule:` and
`workflow_dispatch:` are ignored on other branches, so `scrape.yaml` cannot be
tested by pushing a feature branch. It has to reach `main` first.

**Fixture linking is deterministic, not fuzzy.** Despite `rapidfuzz` being a
dependency, matching compares normalised team tokens within a single kick-off
time. Token containment is only sound inside a kick-off block; applied across a
whole feed it merges "AC Mailand" into "Inter Mailand". Read the
Cross-Bookmaker Matching section of the README before changing `core/matching.py`.

**Ambiguity leaves rows unlinked rather than guessing.** A wrong link silently
corrupts the comparison and is permanent; an unlinked row is merely absent and
relinks on the next scrape. Keep that bias.

**Free tier limits.** Supabase pauses a project after 7 days of inactivity; the
three-hourly cron is what keeps it awake. Storage is capped at 500 MB, and
nothing currently prunes old `sportsbettingodds` rows.

## Conventions

- Comments explain why, not what. Several existing comments record a decision
  and the failure that motivated it; match that register rather than narrating
  the code.
- Line length 88, ruff formatted, `make lint` from the root covers every
  service.
- Migrations use `render_as_batch=True` so the same migration applies to SQLite
  locally and Postgres in production.
