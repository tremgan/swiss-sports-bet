"""Streamlit dashboard comparing cross-bookmaker odds.

The arbitrage maths lives in `core.arbitrage` so that the dashboard and any
other consumer agree on what counts as an opportunity.
"""

import os
from datetime import UTC, datetime
from typing import Any

import pandas as pd
import pytz
import requests
import streamlit as st

from core.arbitrage import ArbitrageResult, analyse

DB_SERVICE_URL = os.getenv("DB_SERVICE_URL", "http://127.0.0.1:8000")
LOCAL_TZ = pytz.timezone("Europe/Zurich")
REQUEST_TIMEOUT_SECONDS = 15

OUTCOME_LABELS = {"team1": "Team 1", "draw": "Draw", "team2": "Team 2"}


def fetch_matches_with_odds() -> list[dict[str, Any]]:
    response = requests.get(
        f"{DB_SERVICE_URL}/matches/with_odds/", timeout=REQUEST_TIMEOUT_SECONDS
    )
    response.raise_for_status()
    return response.json()


def to_local(dt_str: str) -> datetime:
    """Parse a naive-UTC timestamp from the API into Swiss local time."""
    return datetime.fromisoformat(dt_str).replace(tzinfo=UTC).astimezone(LOCAL_TZ)


def fmt_datetime(dt_str: str) -> str:
    return to_local(dt_str).strftime("%a %d %b %Y, %H:%M")


def fmt_timestamp(dt_str: str) -> str:
    return to_local(dt_str).strftime("%H:%M %d/%m/%y")


def render_match(
    match: dict[str, Any],
    bookmaker_odds: dict[str, Any],
    result: ArbitrageResult,
) -> None:
    flag = "🟢" if result.has_arbitrage else "🔴"
    label = f"{flag} {match['match_label']} (margin: {result.margin_pct:.2f}%)"

    with st.expander(label):
        st.markdown(f"**{fmt_datetime(match['match_datetime'])}**")

        odds_df = pd.DataFrame(bookmaker_odds).T
        odds_df.index.name = "Bookmaker"
        odds_df["timestamp"] = odds_df["timestamp"].apply(fmt_timestamp)
        st.markdown("#### All bookmaker odds")
        st.dataframe(odds_df, width="stretch")

        st.markdown("#### Best odds")
        best_df = pd.DataFrame(
            [
                {
                    "Outcome": OUTCOME_LABELS[outcome],
                    "Bookmaker": bookmaker,
                    "Best Odds": price,
                    "Stake": (
                        f"{result.stakes[outcome]:.1%}" if result.stakes else "—"
                    ),
                }
                for outcome, (bookmaker, price) in result.best_odds.items()
            ]
        )
        st.dataframe(best_df, width="stretch", hide_index=True)

        if result.has_arbitrage and result.profit_pct is not None:
            st.success(f"Arbitrage opportunity! Profit: {result.profit_pct:.2f}%")


def render_matches() -> None:
    st.title("🇨🇭 Swiss Sports Bet Dashboard")
    st.markdown("---")
    st.markdown(
        "Made by [Remi Tregan](https://github.com/tremgan) · "
        "[GitHub](https://github.com/tremgan/sports-bet)"
    )

    try:
        data = fetch_matches_with_odds()
    except requests.RequestException as exc:
        st.error(f"Could not reach the odds service at {DB_SERVICE_URL}: {exc}")
        return

    if not data:
        st.info("No matches with odds from multiple bookmakers found.")
        return

    analysed: list[tuple[dict[str, Any], ArbitrageResult]] = []
    for item in data:
        try:
            analysed.append((item, analyse(item["bookmaker_odds"])))
        except ValueError as exc:
            st.warning(f"Skipping {item['match']['match_label']}: {exc}")

    # Tightest book first, so any arbitrage sits at the top.
    analysed.sort(key=lambda pair: pair[1].implied_total)

    for item, result in analysed:
        render_match(item["match"], item["bookmaker_odds"], result)


if __name__ == "__main__":
    render_matches()
