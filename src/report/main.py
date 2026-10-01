"""Renders the cross-bookmaker odds comparison as a static HTML page.

A scheduled scrape has no reader attached, so there is nothing for a server to
serve: the page is written once per run and published as a file. It reads the
same `GET /matches/with_odds/` payload the Streamlit dashboard reads and uses
`core.arbitrage` for the maths, so both agree on what counts as an opportunity.

The page carries its own CSS and no scripts, because GitHub Pages serves it
from a bare directory and a self-contained file cannot half-load.
"""

import os
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import requests
from jinja2 import Environment, FileSystemLoader, select_autoescape

from core.arbitrage import analyse
from core.logging_config import setup_logging

logger = setup_logging("report")

DB_SERVICE_URL = os.getenv("DB_SERVICE_URL", "http://127.0.0.1:8000")
# Both bookmakers are Swiss, so their audience reads kick-offs in Swiss time.
LOCAL_TZ = ZoneInfo("Europe/Zurich")
REQUEST_TIMEOUT_SECONDS = 15
DEFAULT_OUTPUT = Path("dist/index.html")
TEMPLATE_DIR = Path(__file__).parent / "templates"

OUTCOME_LABELS = {"team1": "Home", "draw": "Draw", "team2": "Away"}


def fetch_matches_with_odds(url: str = DB_SERVICE_URL) -> list[dict[str, Any]]:
    response = requests.get(
        f"{url}/matches/with_odds/", timeout=REQUEST_TIMEOUT_SECONDS
    )
    response.raise_for_status()
    return response.json()


def to_local(dt_str: str) -> datetime:
    """Parse a naive-UTC timestamp from the API into Swiss local time."""
    return datetime.fromisoformat(dt_str).replace(tzinfo=UTC).astimezone(LOCAL_TZ)


def build_fixtures(data: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Turn the API payload into view rows, tightest book first.

    A fixture nobody can price on both sides raises out of `analyse`; it is
    dropped with a warning rather than failing the whole page, since one bad
    market should not cost the run its report.
    """
    fixtures: list[dict[str, Any]] = []

    for item in data:
        match = item["match"]
        bookmaker_odds = item["bookmaker_odds"]
        try:
            result = analyse(bookmaker_odds)
        except ValueError as exc:
            logger.warning(f"skipping {match['match_label']}: {exc}")
            continue

        fixtures.append(
            {
                "label": match["match_label"],
                "kickoff": to_local(match["match_datetime"]).strftime(
                    "%a %d %b, %H:%M"
                ),
                "margin_pct": result.margin_pct,
                "has_arbitrage": result.has_arbitrage,
                "profit_pct": result.profit_pct,
                "implied_total": result.implied_total,
                "bookmakers": [
                    {
                        "name": bookmaker,
                        "team1_odds": odds["team1_odds"],
                        "draw_odds": odds["draw_odds"],
                        "team2_odds": odds["team2_odds"],
                        "updated": to_local(odds["timestamp"]).strftime("%H:%M"),
                        # Marks the cell holding the best price for an outcome.
                        "best": {
                            outcome
                            for outcome, (name, _) in result.best_odds.items()
                            if name == bookmaker
                        },
                    }
                    for bookmaker, odds in sorted(bookmaker_odds.items())
                ],
                "best_odds": [
                    {
                        "outcome": OUTCOME_LABELS[outcome],
                        "bookmaker": bookmaker,
                        "price": price,
                        "stake_pct": (
                            result.stakes[outcome] * 100.0 if result.stakes else None
                        ),
                    }
                    for outcome, (bookmaker, price) in result.best_odds.items()
                ],
            }
        )

    # Tightest book first, so any arbitrage sits at the top of the page.
    fixtures.sort(key=lambda fixture: fixture["implied_total"])
    return fixtures


def render(fixtures: list[dict[str, Any]], generated_at: datetime) -> str:
    env = Environment(
        loader=FileSystemLoader(TEMPLATE_DIR),
        autoescape=select_autoescape(["html"]),
        trim_blocks=True,
        lstrip_blocks=True,
    )
    return env.get_template("report.html.j2").render(
        fixtures=fixtures,
        arbitrage_count=sum(1 for f in fixtures if f["has_arbitrage"]),
        generated_at=generated_at.astimezone(LOCAL_TZ).strftime("%d %b %Y, %H:%M %Z"),
    )


def main(argv: list[str] | None = None) -> None:
    argv = sys.argv[1:] if argv is None else argv
    output = Path(argv[0]) if argv else DEFAULT_OUTPUT

    data = fetch_matches_with_odds()
    logger.info(f"fetched {len(data)} matches priced by more than one bookmaker")

    fixtures = build_fixtures(data)
    html = render(fixtures, datetime.now(UTC))

    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(html, encoding="utf-8")
    logger.info(
        f"wrote {output} — {len(fixtures)} fixtures, "
        f"{sum(1 for f in fixtures if f['has_arbitrage'])} with arbitrage"
    )


if __name__ == "__main__":
    main()
