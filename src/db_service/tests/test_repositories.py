from collections.abc import Mapping
from datetime import UTC, datetime, timedelta

from sqlmodel import Session

from core.models import BookmakerMatch, Match, SportsBettingOdds
from repositories import BettingRepository

# (team1_odds, draw_odds, team2_odds, timestamp)
Snapshots = list[tuple[float, float | None, float, datetime]]

# Timestamps are stored as naive UTC, matching what the scrapers write.
NOW = datetime.now(UTC).replace(tzinfo=None)
FRESH = NOW - timedelta(minutes=5)
OLDER_BUT_FRESH = NOW - timedelta(minutes=30)
STALE = NOW - timedelta(hours=3)

KICKOFF = datetime(2026, 3, 25, 18, 0)


def seed_match(
    session: Session,
    label: str,
    kickoff: datetime,
    snapshots_by_bookmaker: Mapping[str, Snapshots],
) -> Match:
    """Create a canonical match with the given odds history per bookmaker."""
    team1, team2 = label.split(" vs ")
    match = Match(match_label=label, match_datetime=kickoff, team1=team1, team2=team2)
    session.add(match)
    session.flush()

    for bookmaker, snapshots in snapshots_by_bookmaker.items():
        bookmaker_match = BookmakerMatch(
            bookmaker=bookmaker,
            match_label=label,
            match_datetime=kickoff,
            match_id=match.id,
        )
        session.add(bookmaker_match)
        session.flush()
        for team1_odds, draw_odds, team2_odds, timestamp in snapshots:
            session.add(
                SportsBettingOdds(
                    bookmaker_match_id=bookmaker_match.id,
                    team1_odds=team1_odds,
                    draw_odds=draw_odds,
                    team2_odds=team2_odds,
                    timestamp=timestamp,
                )
            )

    session.commit()
    return match


def test_returns_nothing_when_there_are_no_matches(session: Session):
    assert BettingRepository(session).get_matches_with_odds() == []


def test_includes_a_match_priced_by_two_bookmakers(session: Session):
    seed_match(
        session,
        "FC Basel vs FC Zurich",
        KICKOFF,
        {
            "Loro": [(2.1, 3.6, 4.0, FRESH)],
            "Swisslos": [(1.8, 3.5, 5.0, FRESH)],
        },
    )

    result = BettingRepository(session).get_matches_with_odds()

    assert len(result) == 1
    assert result[0]["match"].match_label == "FC Basel vs FC Zurich"
    assert set(result[0]["bookmaker_odds"]) == {"Loro", "Swisslos"}
    assert result[0]["bookmaker_odds"]["Loro"]["team1_odds"] == 2.1


def test_excludes_a_match_priced_by_only_one_bookmaker(session: Session):
    seed_match(
        session,
        "FC Basel vs FC Zurich",
        KICKOFF,
        {"Loro": [(2.1, 3.6, 4.0, FRESH)]},
    )

    assert BettingRepository(session).get_matches_with_odds() == []


def test_excludes_a_bookmaker_whose_latest_odds_are_stale(session: Session):
    """A bookmaker that stopped reporting must not pin an old price to a fresh one."""
    seed_match(
        session,
        "FC Basel vs FC Zurich",
        KICKOFF,
        {
            "Loro": [(2.1, 3.6, 4.0, FRESH)],
            "Swisslos": [(1.8, 3.5, 5.0, STALE)],
        },
    )

    # Only Loro is still fresh, which drops the match below two bookmakers.
    assert BettingRepository(session).get_matches_with_odds() == []


def test_returns_only_the_newest_snapshot_per_bookmaker(session: Session):
    seed_match(
        session,
        "FC Basel vs FC Zurich",
        KICKOFF,
        {
            "Loro": [
                (9.9, 9.9, 9.9, OLDER_BUT_FRESH),
                (2.1, 3.6, 4.0, FRESH),
            ],
            "Swisslos": [(1.8, 3.5, 5.0, FRESH)],
        },
    )

    odds = BettingRepository(session).get_matches_with_odds()[0]["bookmaker_odds"]

    assert odds["Loro"]["team1_odds"] == 2.1
    assert odds["Loro"]["timestamp"] == FRESH


def test_orders_by_kick_off_and_paginates_over_matches(session: Session):
    for day in (26, 25, 27):
        seed_match(
            session,
            f"Team {day}A vs Team {day}B",
            datetime(2026, 3, day, 18, 0),
            {
                "Loro": [(2.1, 3.6, 4.0, FRESH)],
                "Swisslos": [(1.8, 3.5, 5.0, FRESH)],
            },
        )
    repo = BettingRepository(session)

    labels = [r["match"].match_label for r in repo.get_matches_with_odds()]
    assert labels == [
        "Team 25A vs Team 25B",
        "Team 26A vs Team 26B",
        "Team 27A vs Team 27B",
    ]

    # limit counts qualifying matches, not joined odds rows.
    page = repo.get_matches_with_odds(limit=2)
    assert [r["match"].match_label for r in page] == labels[:2]
    assert all(len(r["bookmaker_odds"]) == 2 for r in page)

    assert [
        r["match"].match_label for r in repo.get_matches_with_odds(limit=2, offset=2)
    ] == labels[2:]
    assert repo.get_matches_with_odds(offset=3) == []


def test_limit_is_applied_after_filtering_not_before(session: Session):
    """A page of N must return N qualifying matches, not N candidates minus rejects."""
    # Three single-bookmaker matches sort first by kick-off, then one good match.
    for day in (20, 21, 22):
        seed_match(
            session,
            f"Solo {day}A vs Solo {day}B",
            datetime(2026, 3, day, 18, 0),
            {"Loro": [(2.1, 3.6, 4.0, FRESH)]},
        )
    seed_match(
        session,
        "FC Basel vs FC Zurich",
        datetime(2026, 3, 25, 18, 0),
        {
            "Loro": [(2.1, 3.6, 4.0, FRESH)],
            "Swisslos": [(1.8, 3.5, 5.0, FRESH)],
        },
    )

    result = BettingRepository(session).get_matches_with_odds(limit=1)

    assert len(result) == 1
    assert result[0]["match"].match_label == "FC Basel vs FC Zurich"


def test_get_odds_and_bookmaker_matches_paginate(session: Session):
    seed_match(
        session,
        "FC Basel vs FC Zurich",
        KICKOFF,
        {
            "Loro": [(2.1, 3.6, 4.0, OLDER_BUT_FRESH), (2.2, 3.7, 4.1, FRESH)],
            "Swisslos": [(1.8, 3.5, 5.0, FRESH)],
        },
    )
    repo = BettingRepository(session)

    assert len(repo.get_bookmaker_matches()) == 2
    assert len(repo.get_bookmaker_matches(limit=1)) == 1
    assert len(repo.get_odds()) == 3
    assert len(repo.get_odds(limit=2, offset=2)) == 1
    # get_odds is newest first.
    assert repo.get_odds(limit=1)[0].timestamp == FRESH
