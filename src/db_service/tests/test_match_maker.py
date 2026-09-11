from datetime import datetime

import pytest
from sqlmodel import Session, select

import match_maker
from core.models import BookmakerMatch, Match

DATETIME_1 = datetime(2024, 1, 1, 12, 0, 0)
DATETIME_2 = datetime(2024, 1, 1, 18, 0, 0)

LABEL = "Team A vs Team B"


def make_bookmaker_match(
    session: Session, label: str, dt: datetime, bookmaker: str = "Loro"
) -> BookmakerMatch:
    bm = BookmakerMatch(match_label=label, match_datetime=dt, bookmaker=bookmaker)
    session.add(bm)
    session.commit()
    session.refresh(bm)
    return bm


def make_match(session: Session, label: str, dt: datetime) -> Match:
    match = Match(
        match_label=label,
        match_datetime=dt,
        team1=label.split(" vs ")[0],
        team2=label.split(" vs ")[1],
    )
    session.add(match)
    session.commit()
    session.refresh(match)
    return match


# ── find_match ────────────────────────────────────────────────────────────────


def test_find_match_exact(session: Session):
    make_match(session, LABEL, DATETIME_1)
    bm = make_bookmaker_match(session, LABEL, DATETIME_1)

    result = match_maker.find_match(bm, session)

    assert result is not None
    assert result.match_label == LABEL
    assert result.match_datetime == DATETIME_1


def test_find_match_fuzzy(session: Session):
    make_match(session, LABEL, DATETIME_1)
    bm = make_bookmaker_match(
        session, "Taem B vs Team A", DATETIME_1
    )  # reversed order and typo

    result = match_maker.find_match(bm, session)

    assert result is not None
    assert result.match_label == LABEL


def test_find_match_within_time_window(session: Session):
    from datetime import timedelta

    close_dt = DATETIME_1 + timedelta(minutes=30)
    make_match(session, LABEL, DATETIME_1)
    bm = make_bookmaker_match(session, LABEL, close_dt)

    result = match_maker.find_match(bm, session)

    assert result is not None
    assert result.match_label == LABEL


def test_find_match_outside_time_window(session: Session):
    from datetime import timedelta

    far_dt = DATETIME_1 + timedelta(hours=2)
    make_match(session, LABEL, DATETIME_1)
    bm = make_bookmaker_match(session, LABEL, far_dt)

    result = match_maker.find_match(bm, session)

    assert result is None


def test_find_match_no_candidates(session: Session):
    bm = make_bookmaker_match(session, LABEL, DATETIME_1)

    result = match_maker.find_match(bm, session)

    assert result is None


def test_find_match_below_fuzzy_threshold(session: Session):
    make_match(session, LABEL, DATETIME_1)
    bm = make_bookmaker_match(session, "Completely Different Match", DATETIME_1)

    result = match_maker.find_match(bm, session)

    assert result is None


# ── create_match_from_bookmaker_match ─────────────────────────────────────────


def test_create_match_from_bookmaker_match():
    bm = BookmakerMatch(
        match_label=LABEL,
        match_datetime=DATETIME_1,
        bookmaker="Loro",
    )
    match = match_maker.create_match_from_bookmaker_match(bm)

    assert match.match_label == LABEL
    assert match.match_datetime == DATETIME_1
    assert match.team1 == "Team A"
    assert match.team2 == "Team B"


# ── run ───────────────────────────────────────────────────────────────────────


def test_run_creates_new_match_when_no_existing(session: Session):
    make_bookmaker_match(session, LABEL, DATETIME_1)

    match_maker.run(session)

    matches = session.exec(select(Match)).all()
    assert len(matches) == 1
    assert matches[0].match_label == LABEL


def test_run_links_bookmaker_match_to_existing_match(session: Session):
    existing = make_match(session, LABEL, DATETIME_1)
    bm = make_bookmaker_match(session, LABEL, DATETIME_1)

    match_maker.run(session)

    session.refresh(bm)
    assert bm.match_id == existing.id


def test_run_increments_matching_attempts(session: Session):
    bm = make_bookmaker_match(session, LABEL, DATETIME_1)
    assert bm.matching_attempts == 0

    match_maker.run(session)

    session.refresh(bm)
    assert bm.matching_attempts == 1


def test_run_skips_already_matched(session: Session):
    existing = make_match(session, LABEL, DATETIME_1)
    bm = make_bookmaker_match(session, LABEL, DATETIME_1)
    bm.match_id = existing.id
    session.add(bm)
    session.commit()

    match_maker.run(session)

    matches = session.exec(select(Match)).all()
    assert len(matches) == 1  # no new match created


def test_run_skips_after_max_attempts(session: Session):
    bm = make_bookmaker_match(session, "Unresolvable Match vs Nobody", DATETIME_1)
    bm.matching_attempts = 3
    session.add(bm)
    session.commit()

    match_maker.run(session)

    matches = session.exec(select(Match)).all()
    assert len(matches) == 0  # no match created, was skipped


def test_run_two_bookmaker_matches_same_game_links_to_same_match(session: Session):
    bm1 = make_bookmaker_match(session, LABEL, DATETIME_1, bookmaker="Loro")
    bm2 = make_bookmaker_match(session, LABEL, DATETIME_1, bookmaker="Swisslos")

    match_maker.run(session)

    session.refresh(bm1)
    session.refresh(bm2)
    matches = session.exec(select(Match)).all()
    assert len(matches) == 1
    assert bm1.match_id == bm2.match_id


# ── real cross-bookmaker labels ───────────────────────────────────────────────

# Captured from live runs of both scrapers. Swisslos gives full club names,
# Loro short ones; these pairs are the reason the scorer is token_set_ratio.
REAL_LABEL_PAIRS = [
    ("FC St. Gallen 1879 vs FC Sion", "St. Gallen vs Sion"),
    ("FC Thun vs Grasshopper Club Zurich", "Thun vs Grasshopper"),
    ("FC Lugano vs Young Boys Bern", "Lugano vs Young Boys"),
]


@pytest.mark.parametrize(("swisslos_label", "loro_label"), REAL_LABEL_PAIRS)
def test_real_bookmaker_labels_match(
    session: Session, swisslos_label: str, loro_label: str
):
    """The two scrapers must reconcile onto one canonical match."""
    make_match(session, swisslos_label, DATETIME_1)
    bm = make_bookmaker_match(session, loro_label, DATETIME_1, bookmaker="Loro")

    result = match_maker.find_match(bm, session)

    assert result is not None, f"{loro_label!r} failed to match {swisslos_label!r}"
    assert result.match_label == swisslos_label


def test_fixture_sharing_one_team_does_not_match(session: Session):
    """token_set_ratio is generous, so the threshold must reject partial overlap."""
    make_match(session, "Levante vs FC Barcelona", DATETIME_1)
    bm = make_bookmaker_match(session, "FC Barcelona vs Real Madrid CF", DATETIME_1)

    assert match_maker.find_match(bm, session) is None


def test_both_scrapers_link_to_one_match_end_to_end(session: Session):
    """The pipeline's actual goal: two bookmakers, one canonical match."""
    make_bookmaker_match(
        session, "FC Thun vs Grasshopper Club Zurich", DATETIME_1, bookmaker="Swisslos"
    )
    make_bookmaker_match(session, "Thun vs Grasshopper", DATETIME_1, bookmaker="Loro")

    match_maker.run(session)

    matches = session.exec(select(Match)).all()
    bms = session.exec(select(BookmakerMatch)).all()
    assert len(matches) == 1
    assert {bm.match_id for bm in bms} == {matches[0].id}
