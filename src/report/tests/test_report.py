from datetime import UTC, datetime

from main import build_fixtures, render

ARBITRAGE_PAYLOAD = [
    {
        "match": {
            "match_label": "Deutschland vs Serbien",
            "match_datetime": "2026-10-01T18:45:00",
        },
        "bookmaker_odds": {
            "Swisslos": {
                "team1_odds": 1.8,
                "draw_odds": 3.5,
                "team2_odds": 5.0,
                "timestamp": "2026-10-01T09:00:00",
            },
            "Loro": {
                "team1_odds": 2.1,
                "draw_odds": 3.6,
                "team2_odds": 4.0,
                "timestamp": "2026-10-01T09:05:00",
                "url": "https://jeux.loro.ch/sports/fr/sportif/evenement/74820",
            },
        },
    }
]

FLAT_PAYLOAD = [
    {
        "match": {"match_label": "A vs B", "match_datetime": "2026-10-01T18:45:00"},
        "bookmaker_odds": {
            "Loro": {
                "team1_odds": 1.5,
                "draw_odds": 3.0,
                "team2_odds": 3.0,
                "timestamp": "2026-10-01T09:00:00",
            },
        },
    }
]


def test_build_fixtures_marks_arbitrage_and_the_bookmaker_holding_each_best_price():
    (fixture,) = build_fixtures(ARBITRAGE_PAYLOAD)

    assert fixture["has_arbitrage"]
    assert fixture["margin_pct"] < 0
    # Loro prices home and draw better, Swisslos the away side.
    by_name = {b["name"]: b["best"] for b in fixture["bookmakers"]}
    assert by_name["Loro"] == {"team1", "draw"}
    assert by_name["Swisslos"] == {"team2"}
    assert sum(b["stake_pct"] for b in fixture["best_odds"]) == 100.0


def test_build_fixtures_leaves_stakes_empty_without_arbitrage():
    (fixture,) = build_fixtures(FLAT_PAYLOAD)

    assert not fixture["has_arbitrage"]
    assert fixture["margin_pct"] > 0
    assert all(b["stake_pct"] is None for b in fixture["best_odds"])


def test_build_fixtures_sorts_the_tightest_book_first():
    fixtures = build_fixtures(FLAT_PAYLOAD + ARBITRAGE_PAYLOAD)

    assert [f["label"] for f in fixtures] == ["Deutschland vs Serbien", "A vs B"]


def test_build_fixtures_drops_an_unpriceable_market_rather_than_failing():
    """One broken market must not cost the run its whole page."""
    unpriceable = [
        {
            "match": {"match_label": "C vs D", "match_datetime": "2026-10-01T18:45:00"},
            "bookmaker_odds": {"Loro": {"timestamp": "2026-10-01T09:00:00"}},
        }
    ]

    assert build_fixtures(unpriceable + ARBITRAGE_PAYLOAD) == build_fixtures(
        ARBITRAGE_PAYLOAD
    )


def test_build_fixtures_lays_the_columns_out_as_1x2_holding_the_best_price():
    """The collapsed row shows the best price available for each outcome."""
    (fixture,) = build_fixtures(ARBITRAGE_PAYLOAD)

    assert [c["key"] for c in fixture["columns"]] == ["1", "X", "2"]
    # Loro prices home and draw better, Swisslos the away side.
    assert [c["price"] for c in fixture["columns"]] == [2.1, 3.6, 5.0]
    assert [c["bookmaker"] for c in fixture["columns"]] == ["Loro", "Loro", "Swisslos"]


def test_render_is_self_contained_and_shows_the_fixture():
    html = render(build_fixtures(ARBITRAGE_PAYLOAD), datetime(2026, 10, 1, tzinfo=UTC))

    assert "Deutschland vs Serbien" in html
    assert "<script" not in html
    assert "src=" not in html  # no external asset can fail to load
    assert "4.83" in html  # the guaranteed profit, to two places


def test_render_carries_a_theme_toggle_that_needs_no_javascript():
    """The page must stay scriptless, so the toggle is a checkbox plus :has()."""
    html = render(build_fixtures(ARBITRAGE_PAYLOAD), datetime(2026, 10, 1, tzinfo=UTC))

    assert 'id="theme"' in html
    assert ":root:has(#theme:checked)" in html
    assert "<script" not in html


def test_render_says_so_when_nothing_is_paired():
    html = render([], datetime(2026, 10, 1, tzinfo=UTC))

    assert "No fixture is currently priced by more than one bookmaker." in html


def test_render_links_a_bookmaker_to_its_own_page_for_the_fixture():
    html = render(build_fixtures(ARBITRAGE_PAYLOAD), datetime(2026, 10, 1, tzinfo=UTC))

    assert 'href="https://jeux.loro.ch/sports/fr/sportif/evenement/74820"' in html
    assert 'rel="noopener noreferrer nofollow"' in html


def test_render_leaves_an_unlinked_bookmaker_as_plain_text():
    """Swisslos carries no url in the payload above, so it must not gain a link."""
    html = render(build_fixtures(ARBITRAGE_PAYLOAD), datetime(2026, 10, 1, tzinfo=UTC))

    assert "Swisslos" in html
    assert html.count('<a class="book"') == 1
