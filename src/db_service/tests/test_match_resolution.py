"""Linking a bookmaker's fixture to its canonical match, at write time."""

from datetime import datetime, timedelta

import pytest
from core.matching import team_key
from core.models import BookmakerMatch, BookmakerMatchCreate, Match
from repositories import BettingRepository
from sqlmodel import Session, select

# Naive UTC, matching what the scrapers write.
KICKOFF = datetime(2026, 9, 12, 16, 0)  # noqa: DTZ001


def post(
    repo: BettingRepository, bookmaker: str, home: str, away: str, kickoff=KICKOFF
):
    return repo.create_bookmaker_match(
        BookmakerMatchCreate(
            bookmaker=bookmaker,
            match_label=f"{home} vs {away}",
            match_datetime=kickoff,
            team1=home,
            team2=away,
        )
    )


def seed_fragment(session: Session, home: str, away: str) -> Match:
    """Insert a canonical match directly, bypassing the resolver."""
    match = Match(
        match_label=f"{home} vs {away}",
        match_datetime=KICKOFF,
        team1=home,
        team2=away,
        home_key=team_key(home),
        away_key=team_key(away),
    )
    session.add(match)
    session.flush()
    return match


# ── the core behaviour ───────────────────────────────────────────────────────


def test_two_bookmakers_naming_a_fixture_differently_link_to_one_match(
    session: Session,
):
    """The whole point: Swisslos' full names and Loro's short ones are one fixture."""
    repo = BettingRepository(session)

    loro = post(repo, "Loro", "Thun", "Grasshopper")
    swisslos = post(repo, "Swisslos", "FC Thun", "Grasshopper Club Zurich")

    assert loro.match_id is not None
    assert loro.match_id == swisslos.match_id
    assert len(session.exec(select(Match)).all()) == 1


def test_linking_does_not_depend_on_arrival_order(session: Session):
    """Identity is derived, so whoever scrapes first cannot change the outcome."""
    repo = BettingRepository(session)

    swisslos = post(repo, "Swisslos", "FC Thun", "Grasshopper Club Zurich")
    loro = post(repo, "Loro", "Thun", "Grasshopper")

    assert swisslos.match_id == loro.match_id
    assert len(session.exec(select(Match)).all()) == 1


def test_identity_is_normalised_not_the_raw_label(session: Session):
    repo = BettingRepository(session)
    post(repo, "Swisslos", "FC Thun", "Grasshopper Club Zurich")
    post(repo, "Loro", "Thun", "Grasshopper")

    # The key is the creating bookmaker's normalised form; matching itself runs
    # on the team columns, so the key only has to be stable, not minimal.
    match = session.exec(select(Match)).one()
    assert match.home_key == team_key("FC Thun") == team_key("Thun")


def test_unrelated_fixtures_stay_separate(session: Session):
    repo = BettingRepository(session)

    a = post(repo, "Loro", "Thun", "Grasshopper")
    b = post(repo, "Loro", "St. Gallen", "Sion")

    assert a.match_id != b.match_id
    assert len(session.exec(select(Match)).all()) == 2


def test_reposting_the_same_fixture_is_idempotent(session: Session):
    repo = BettingRepository(session)

    first = post(repo, "Loro", "Thun", "Grasshopper")
    second = post(repo, "Loro", "Thun", "Grasshopper")

    assert first.id == second.id
    assert len(session.exec(select(Match)).all()) == 1
    assert len(session.exec(select(BookmakerMatch)).all()) == 1


# ── the safety rules ─────────────────────────────────────────────────────────


def test_same_club_different_squads_do_not_link(session: Session):
    """A women's side must not be absorbed into the men's."""
    repo = BettingRepository(session)

    mens = post(repo, "Loro", "FC Zurich", "FC Basel")
    womens = post(repo, "Swisslos", "FC Zurich (W)", "FC Basel (W)")

    assert mens.match_id != womens.match_id


def test_a_kickoff_apart_by_more_than_the_tolerance_does_not_link(session: Session):
    repo = BettingRepository(session)

    early = post(repo, "Loro", "Thun", "Grasshopper")
    late = post(
        repo,
        "Swisslos",
        "FC Thun",
        "Grasshopper Club Zurich",
        kickoff=KICKOFF + timedelta(hours=3),
    )

    assert early.match_id != late.match_id


def test_small_kickoff_disagreement_still_links(session: Session):
    repo = BettingRepository(session)

    a = post(repo, "Loro", "Thun", "Grasshopper")
    b = post(
        repo,
        "Swisslos",
        "FC Thun",
        "Grasshopper Club Zurich",
        kickoff=KICKOFF + timedelta(minutes=5),
    )

    assert a.match_id == b.match_id


def test_ambiguity_is_refused_rather_than_guessed(session: Session):
    """A wrong link is permanent and silent; an unlinked row is merely absent."""
    # Two canonical fixtures at one kick-off that a short name matches both of.
    # Seeded directly, because posting the second through the resolver would
    # link it to the first — containment cannot tell them apart either.
    seed_fragment(session, "Inter Mailand", "AS Rom")
    seed_fragment(session, "AC Mailand", "Lazio Rom")

    ambiguous = post(BettingRepository(session), "Swisslos", "Mailand", "Rom")

    assert ambiguous.match_id is None


# ── merging fragments ────────────────────────────────────────────────────────


def test_fragments_are_not_merged_automatically(session: Session):
    """Two compatible canonical rows are indistinguishable from two fixtures.

    The resolver must refuse rather than fuse them; repair is an explicit act.
    """
    seed_fragment(session, "Grasshopper Club Zurich", "Thun")
    seed_fragment(session, "Grasshopper", "FC Thun")

    bridge = post(BettingRepository(session), "Loro", "Grasshopper", "Thun")

    assert bridge.match_id is None
    assert len(session.exec(select(Match)).all()) == 2


def test_merge_matches_repoints_bookmakers_and_removes_the_duplicate(
    session: Session,
):
    """The explicit repair, performed by hand on what the resolver refuses."""
    repo = BettingRepository(session)
    keep = seed_fragment(session, "Grasshopper Club Zurich", "Thun")
    drop = seed_fragment(session, "Grasshopper", "FC Thun")

    orphan = BookmakerMatch(
        bookmaker="Swisslos",
        match_label="Grasshopper vs FC Thun",
        match_datetime=KICKOFF,
        team1="Grasshopper",
        team2="FC Thun",
        match_id=drop.id,
    )
    session.add(orphan)
    session.flush()

    repo.merge_matches(keep, drop)

    session.refresh(orphan)
    assert orphan.match_id == keep.id
    assert session.get(Match, drop.id) is None


def test_merging_lets_a_previously_ambiguous_row_link(session: Session):
    """After repair the fixture resolves, so the dashboard sees it again."""
    repo = BettingRepository(session)
    keep = seed_fragment(session, "Grasshopper Club Zurich", "Thun")
    drop = seed_fragment(session, "Grasshopper", "FC Thun")
    repo.merge_matches(keep, drop)

    bridge = post(repo, "Loro", "Grasshopper", "Thun")

    assert bridge.match_id == keep.id


def test_reposting_an_unlinked_row_retries_resolution(session: Session):
    """The row already on disk must heal, not just rows that arrive later.

    Re-posting is the only retry left now that the batch pass is gone, so a
    fixture refused once and repaired afterwards has to come back through the
    very same (bookmaker, label, datetime) the scraper keeps sending.
    """
    repo = BettingRepository(session)
    keep = seed_fragment(session, "Grasshopper Club Zurich", "Thun")
    drop = seed_fragment(session, "Grasshopper", "FC Thun")

    refused = post(repo, "Loro", "Grasshopper", "Thun")
    assert refused.match_id is None

    repo.merge_matches(keep, drop)
    session.commit()

    relinked = post(repo, "Loro", "Grasshopper", "Thun")

    assert relinked.id == refused.id, "the same row, not a second one"
    assert relinked.match_id == keep.id


def test_a_row_that_is_still_ambiguous_stays_unlinked_on_repost(session: Session):
    """Retrying must not decay into guessing once the block is still ambiguous."""
    repo = BettingRepository(session)
    seed_fragment(session, "Grasshopper Club Zurich", "Thun")
    seed_fragment(session, "Grasshopper", "FC Thun")

    first = post(repo, "Loro", "Grasshopper", "Thun")
    second = post(repo, "Loro", "Grasshopper", "Thun")

    assert first.id == second.id
    assert second.match_id is None


def test_losing_the_race_to_create_a_match_links_to_the_winner(
    session: Session, monkeypatch
):
    """Two scrapers can create one identity at once; one INSERT has to lose.

    The savepoint is what keeps that survivable: the loser links to the winner's
    row instead of taking the whole transaction down with it.
    """
    repo = BettingRepository(session)
    winner = seed_fragment(session, "Thun", "Grasshopper")

    # Stand in for the other process. Its row is on disk, but this resolver's
    # SELECT ran before it landed, so nothing looks like a candidate and the
    # conflict only surfaces on INSERT — which is the race itself.
    monkeypatch.setattr("repositories.same_team", lambda a, b: False)

    posted = post(repo, "Loro", "Thun", "Grasshopper")

    assert posted.match_id == winner.id
    assert len(session.exec(select(Match)).all()) == 1


@pytest.mark.parametrize("bookmaker", ["Loro", "Swisslos"])
def test_a_lone_bookmaker_still_gets_a_canonical_match(
    session: Session, bookmaker: str
):
    repo = BettingRepository(session)

    created = post(repo, bookmaker, "Thun", "Grasshopper")

    assert created.match_id is not None
