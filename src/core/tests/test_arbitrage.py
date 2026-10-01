import pytest
from core.arbitrage import analyse

# Best prices across these two books are 2.1 / 3.6 / 5.0, whose implied
# probabilities sum to ~0.954 — an arbitrage.
ARBITRAGE_ODDS = {
    "Bookmaker A": {"team1_odds": 2.1, "draw_odds": 3.6, "team2_odds": 4.0},
    "Bookmaker B": {"team1_odds": 1.8, "draw_odds": 3.5, "team2_odds": 5.0},
}

# Two copies of the same overrounding book: no combination beats 1.0.
NO_ARBITRAGE_ODDS = {
    "Bookmaker A": {"team1_odds": 2.0, "draw_odds": 3.3, "team2_odds": 3.6},
    "Bookmaker B": {"team1_odds": 1.95, "draw_odds": 3.2, "team2_odds": 3.5},
}


def test_picks_the_best_price_per_outcome():
    result = analyse(ARBITRAGE_ODDS)

    assert result.best_odds["team1"] == ("Bookmaker A", 2.1)
    assert result.best_odds["draw"] == ("Bookmaker A", 3.6)
    assert result.best_odds["team2"] == ("Bookmaker B", 5.0)


def test_detects_arbitrage():
    result = analyse(ARBITRAGE_ODDS)

    assert result.implied_total == pytest.approx(1 / 2.1 + 1 / 3.6 + 1 / 5.0)
    assert result.has_arbitrage
    assert result.margin_pct < 0


def test_stakes_split_the_bankroll_and_equalise_the_payout():
    result = analyse(ARBITRAGE_ODDS)

    assert result.stakes is not None
    assert sum(result.stakes.values()) == pytest.approx(1.0)

    # Whichever outcome lands, the return on the whole bankroll is the same.
    payouts = [
        result.stakes[outcome] * price
        for outcome, (_, price) in result.best_odds.items()
    ]
    assert payouts == pytest.approx([payouts[0]] * len(payouts))
    assert result.profit_pct == pytest.approx((payouts[0] - 1) * 100)
    assert result.profit_pct is not None and result.profit_pct > 0


def test_no_arbitrage_reports_a_positive_margin_and_no_stakes():
    result = analyse(NO_ARBITRAGE_ODDS)

    assert not result.has_arbitrage
    assert result.implied_total > 1.0
    assert result.margin_pct > 0
    assert result.stakes is None
    assert result.profit_pct is None


def test_two_way_market_is_analysed_rather_than_crashing():
    """Every bookmaker reporting a null draw used to raise from an empty max()."""
    result = analyse(
        {
            "Bookmaker A": {"team1_odds": 1.9, "draw_odds": None, "team2_odds": 2.2},
            "Bookmaker B": {"team1_odds": 2.05, "draw_odds": None, "team2_odds": 2.0},
        }
    )

    assert "draw" not in result.best_odds
    assert result.best_odds["team1"] == ("Bookmaker B", 2.05)
    assert result.implied_total == pytest.approx(1 / 2.05 + 1 / 2.2)
    assert result.has_arbitrage


def test_draw_priced_by_only_one_bookmaker_still_counts():
    result = analyse(
        {
            "Bookmaker A": {"team1_odds": 2.1, "draw_odds": 3.6, "team2_odds": 4.0},
            "Bookmaker B": {"team1_odds": 1.8, "draw_odds": None, "team2_odds": 5.0},
        }
    )

    assert result.best_odds["draw"] == ("Bookmaker A", 3.6)


def test_extra_keys_in_the_payload_are_ignored():
    """The db_service payload also carries a timestamp."""
    result = analyse(
        {
            "Bookmaker A": {
                "team1_odds": 2.1,
                "draw_odds": 3.6,
                "team2_odds": 4.0,
                "timestamp": "2024-01-01T10:00:00",
            }
        }
    )

    assert result.best_odds["team1"] == ("Bookmaker A", 2.1)


def test_rejects_empty_input():
    with pytest.raises(ValueError, match="no bookmaker odds"):
        analyse({})


def test_rejects_a_market_missing_a_required_outcome():
    with pytest.raises(ValueError, match="team2"):
        analyse({"Bookmaker A": {"team1_odds": 2.0, "team2_odds": None}})
