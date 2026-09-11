"""Shared library for the swiss-sports-bet services.

Submodules are imported directly (`from core.models import Match`) rather than
re-exported here, so that importing the schema does not drag in the HTTP stack.

- :mod:`core.models` — SQLModel tables shared by every service
- :mod:`core.arbitrage` — cross-bookmaker arbitrage detection
- :mod:`core.scraper` — the scrape/publish/reconcile runtime
- :mod:`core.logging_config` — one stdout logging setup for all services
"""
