# core

Shared library for the [swiss-sports-bet](../../README.md) services. Installed by
every other service as a local editable dependency via uv.

| Module | Purpose |
|---|---|
| `core.models` | SQLModel tables — `Match`, `BookmakerMatch`, `SportsBettingOdds` — plus the odds plausibility validator |
| `core.arbitrage` | `analyse()` finds the best price per outcome across bookmakers and reports margin, stakes and profit |
| `core.scraper` | The scrape → publish → reconcile loop both scrapers run, with timeouts and retries |
| `core.logging_config` | `setup_logging()`, one stdout logging setup for all services |

`core.models` and `core.arbitrage` are dependency-light on purpose: the arbitrage
math works on plain mappings, so neither the ORM nor a numerics stack is pulled
into the scraper images.

```bash
uv sync --all-groups
uv run pytest
```
