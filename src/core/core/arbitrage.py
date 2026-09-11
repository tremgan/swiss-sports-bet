"""Cross-bookmaker arbitrage detection for 1X2 football markets.

A bookmaker's own book always overrounds: the implied probabilities of its
outcomes sum to more than 1, and the excess is its margin. Taking the *best*
price for each outcome across several bookmakers can push that sum below 1, at
which point a stake split proportional to the implied probabilities returns the
same payout whichever way the match goes — a guaranteed profit.

This module works on plain mappings shaped like the `bookmaker_odds` payload
the db_service returns, so it stays free of both the ORM and pandas.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

# Maps an outcome to the odds key carrying its price.
OUTCOME_FIELDS: Mapping[str, str] = {
    "team1": "team1_odds",
    "draw": "draw_odds",
    "team2": "team2_odds",
}

# team1/team2 must be priced for a market to be usable; the draw is optional so
# that two-way markets still analyse instead of blowing up.
REQUIRED_OUTCOMES = ("team1", "team2")

# Values are Any because the db_service payload also carries a timestamp;
# only the odds fields are read.
BookmakerOdds = Mapping[str, Mapping[str, Any]]


@dataclass(frozen=True)
class ArbitrageResult:
    """The best available price per outcome and what it implies."""

    best_odds: dict[str, tuple[str, float]]
    """Outcome -> (bookmaker offering the best price, that price)."""

    implied_total: float
    """Sum of 1/odds over the best prices. Below 1.0 means arbitrage."""

    has_arbitrage: bool

    stakes: dict[str, float] | None
    """Fraction of bankroll per outcome, summing to 1. None without arbitrage."""

    profit_pct: float | None
    """Guaranteed return on total stake, as a percent. None without arbitrage."""

    @property
    def margin_pct(self) -> float:
        """Combined bookmaker margin, as a percent. Negative when arbitraging."""
        return (self.implied_total - 1.0) * 100.0


def _best_price(bookmaker_odds: BookmakerOdds, field: str) -> tuple[str, float] | None:
    """Highest odds offered for one outcome, or None if nobody prices it."""
    priced: list[tuple[str, float]] = []
    for bookmaker, odds in bookmaker_odds.items():
        price = odds.get(field)
        if price is not None:
            priced.append((bookmaker, float(price)))
    if not priced:
        return None
    return max(priced, key=lambda pair: pair[1])


def analyse(bookmaker_odds: BookmakerOdds) -> ArbitrageResult:
    """Find the best price per outcome and report whether they arbitrage.

    Args:
        bookmaker_odds: Bookmaker name -> a mapping with `team1_odds`,
            `team2_odds` and optionally `draw_odds`. Extra keys are ignored,
            so the db_service payload (which also carries `timestamp`) works
            unchanged.

    Raises:
        ValueError: if no bookmaker prices both `team1` and `team2`.
    """
    if not bookmaker_odds:
        raise ValueError("no bookmaker odds to analyse")

    best_odds: dict[str, tuple[str, float]] = {}
    for outcome, field in OUTCOME_FIELDS.items():
        best = _best_price(bookmaker_odds, field)
        if best is not None:
            best_odds[outcome] = best

    missing = [o for o in REQUIRED_OUTCOMES if o not in best_odds]
    if missing:
        raise ValueError(f"no odds available for outcome(s): {', '.join(missing)}")

    implied = {outcome: 1.0 / price for outcome, (_, price) in best_odds.items()}
    implied_total = sum(implied.values())
    has_arbitrage = implied_total < 1.0

    stakes = None
    profit_pct = None
    if has_arbitrage:
        # Staking in proportion to implied probability equalises the payout
        # across outcomes, so the return is the same however the match ends.
        stakes = {outcome: p / implied_total for outcome, p in implied.items()}
        profit_pct = (1.0 / implied_total - 1.0) * 100.0

    return ArbitrageResult(
        best_odds=best_odds,
        implied_total=implied_total,
        has_arbitrage=has_arbitrage,
        stakes=stakes,
        profit_pct=profit_pct,
    )
