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

Everything described here is merged to `main` and running. The repository is
public, which it has to be for Pages on a free account.

- The pipeline has completed end to end on Actions three times, publishing to
  https://tremgan.github.io/swiss-sports-bet/ on each.
- Supabase Postgres holds the data, migrated to head. One repository secret,
  `DATABASE_URL`, carries the session pooler URI.
- Pages is configured with `build_type: workflow`, so there is no `gh-pages`
  branch and nothing in the tree holds the built page.

Two things are worth knowing about how it got here, because the repository no
longer shows them. It used to deploy to a VPS over SSH with docker-compose, and
it used to serve a Streamlit dashboard. Both are gone: the job is a batch run
that outlives nothing, so there is no container to orchestrate and no server to
keep up. `src/report/` renders what the dashboard used to display.

The arbitrage path is live but rarely fires. Two bookmakers seldom disagree
enough to open a gap, so expect `0 arbitrage` on the page most of the time.
`core.arbitrage` returns `stakes = None` whenever the book is not beatable,
which is by design rather than a fault.

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

Adding or removing a service means touching three places: `Makefile`
(`SERVICES`), `.github/workflows/test.yaml` (the matrix), and this file.

## Commands

```bash
make check       # lint + typecheck + test, matching CI
make sync        # uv sync every service
```

Run anything else from inside the service directory, since each has its own
venv. `make typecheck` loops because pyright needs each service's own venv to
resolve its dependencies.

Tests at last count: 120 across core (48), db_service (42),
loro_scrape_service (15), swisslos_scrape_service (5) and report (10).

## Gotchas

**There is no `.env`, on purpose.** Actions supplies `DATABASE_URL` from
repository secrets, so nothing in CI ever wanted one. The file that used to sit
at the repo root carried production, and `load_dotenv()` walks *up* from the
working directory — so anything started anywhere inside the repo, a test run or
a stray `uvicorn` included, silently got production credentials. One such server
was still listening on a local port hours later and took a full scrape.
`config.py` now pins the lookup to its own directory, and a local run with no
`DATABASE_URL` gets no engine at all. Name the database you mean:
`DATABASE_URL="sqlite:///dev.db" uv run uvicorn main:app`.

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

**Supabase's row-level-security advisory on these tables is a false positive.**
It flags RLS as disabled on all four public tables and warns that anyone with
the anon key can read or write every row. Nothing can: Alembic creates the
tables as `postgres`, and `anon`, `authenticated` and `service_role` hold no
privilege on any of them, so PostgREST refuses a request before RLS is ever
consulted. The advisory assumes the usual Supabase project, where those grants
exist and RLS is the only thing behind the key. Here the one way in is
`DATABASE_URL`. Switching RLS on would not break the pipeline — db_service
connects as `postgres`, which owns the tables and bypasses RLS — so turn it on
if anything ever starts talking to this project with the publishable key, which
is public by design.

**Neither feed carries a link, and the two are recovered differently.**
`bookmakermatch.url` is what the report links a bookmaker's name to, and each
scraper earns it its own way. Loro's sportsbook routes on the event id alone —
`/sports/fr/sportif/evenement/<id>` — so the sport and fixture slugs a real URL
also carries can be dropped; French because the de-CH route redirects to
`/not-found` whatever locale the feed is scraped in. Swisslos needs a `?t=`
round id that is nowhere in the WebSocket frames, so the scraper reads the
anchors out of the DOM instead and keys them on a `<home>-vs-<away>` slug. That
slug is the weak joint: the page shortens some names ("Real Sociedad San
Sebastian B" is `u21-real-sociedad`), and those fixtures go unlinked rather than
linked to a neighbour. Expect one or two of twenty to have no Swisslos link.

A bookmaker match is inserted once and re-posted every run, so
`create_bookmaker_match` takes the url from the repeat post rather than
returning the existing row untouched. Without that the column fills only for
fixtures first seen after the migration, and a database with history — which
production was — publishes almost no links at all. That is how this shipped
broken the first time.

**The published page must stay scriptless.** A test rejects any `<script` or
`src=` in the rendered output, so anything interactive has to be done in CSS.
The light/dark toggle is a hidden checkbox that `:root:has(#theme:checked)`
reacts to, which is why the theme choice does not survive a reload. Do not
reach for JavaScript without deciding to drop that test first.

**The runner image is pinned, the way ruff is not.** Every job says
`ubuntu-24.04` rather than `ubuntu-latest`, which GitHub migrates to Ubuntu 26
from 19 October 2026. The Swisslos scraper installs Chromium with
`playwright install --with-deps` on the runner, so the OS is a real dependency
of the scrape rather than a detail, and a silent image bump would land first on
the scheduled job — where a failure is a missing page, not a red check anyone
is watching. Bump it deliberately when there is a reason to.

**Ruff has no config file, on purpose.** Its defaults in 0.16 are a broad rule
set, broader than the curated `select` the repo used to carry. That means the
lint surface depends on the installed ruff version, and CI pulls the latest. If
a future version adds rules and CI goes red on untouched code, pin ruff in CI
rather than reintroducing a config.

## Conventions

- Comments explain why, not what. Several existing comments record a decision
  and the failure that motivated it; match that register rather than narrating
  the code.
- Ruff runs with no configuration file, on its defaults, which in ruff 0.16
  is a broad rule set. Deliberate violations carry an inline `# noqa` with a
  reason rather than a config exemption. Kick-offs are stored naive in UTC,
  so `DTZ001` is suppressed at every test fixture that builds one.
- Migrations use `render_as_batch=True` so the same migration applies to SQLite
  locally and Postgres in production.
