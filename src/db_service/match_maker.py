"""Reconciles incoming bookmaker matches against canonical `Match` rows.

Loro and Swisslos name teams differently and report slightly different kick-off
times, so a direct join is impossible. Each unlinked `BookmakerMatch` is
resolved against candidates inside a time window, first by exact label match and
then by fuzzy ratio, and a new canonical `Match` is created when nothing fits.
"""

import logging
from collections.abc import Sequence
from datetime import timedelta

from rapidfuzz import fuzz
from sqlmodel import Session, col, select

from core.models import BookmakerMatch, Match

logger = logging.getLogger("db_service.match_maker")


TIME_DELTA_FOR_MATCHING = timedelta(hours=1)

# token_set_ratio rather than token_sort_ratio because bookmakers disagree on
# how much of a club's name to include: Swisslos says "FC Thun vs Grasshopper
# Club Zurich" where Loro says "Thun vs Grasshopper". Comparing on the shared
# token set scores those 100, where token_sort_ratio scores 72 and drops them.
# The threshold is high because genuine matches score at or near 100, while a
# fixture that merely shares one team peaks around 79.
TOKEN_SET_RATIO_THRESHOLD = 85
# Give up on a bookmaker match after this many failed reconciliation passes.
MAX_MATCHING_ATTEMPTS = 3


def find_match(bookmaker_match: BookmakerMatch, session: Session) -> Match | None:
    """Find the best `Match` for a `BookmakerMatch`, or None if none is close enough."""

    logger.info(
        f"Starting match search for bookmaker match: "
        f"{bookmaker_match.match_label} at {bookmaker_match.match_datetime}"
    )

    time_window_start = bookmaker_match.match_datetime - TIME_DELTA_FOR_MATCHING
    time_window_end = bookmaker_match.match_datetime + TIME_DELTA_FOR_MATCHING

    candidates: Sequence[Match] = session.exec(
        select(Match).where(
            Match.match_datetime >= time_window_start,
            Match.match_datetime <= time_window_end,
        )
    ).all()

    logger.info(f"Found {len(candidates)} candidate matches within time window")

    if not candidates:
        logger.info("No candidate matches found within time window")
        return None

    exact = next(
        (
            c
            for c in candidates
            if c.match_label == bookmaker_match.match_label
            and c.match_datetime == bookmaker_match.match_datetime
        ),
        None,
    )
    # There could in principle be several exact matches, but Match has a unique
    # constraint on (match_label, match_datetime), so there is at most one.
    if exact:
        logger.info(f"Found exact match: {exact.match_label} at {exact.match_datetime}")
        return exact

    # If no exact match, use fuzzy matching
    logger.info("No exact match found, performing fuzzy matching")
    best_candidate = max(
        candidates,
        key=lambda c: fuzz.token_set_ratio(c.match_label, bookmaker_match.match_label),
    )
    best_score = fuzz.token_set_ratio(
        best_candidate.match_label, bookmaker_match.match_label
    )

    logger.info(
        f"Best fuzzy match: '{best_candidate.match_label}' with score {best_score}"
    )

    # Above the threshold the best candidate wins; below it, nothing matches.
    if best_score <= TOKEN_SET_RATIO_THRESHOLD:
        logger.warning(
            f"Best match score {best_score} is below threshold "
            f"{TOKEN_SET_RATIO_THRESHOLD}, no match found"
        )
        return None

    logger.info(f"Match found with score {best_score}: {best_candidate.match_label}")
    return best_candidate


def create_match_from_bookmaker_match(bookmaker_match: BookmakerMatch) -> Match:
    return Match(
        match_label=bookmaker_match.match_label,
        match_datetime=bookmaker_match.match_datetime,
        team1=bookmaker_match.match_label.split(" vs ")[0],
        team2=bookmaker_match.match_label.split(" vs ")[1],
    )


def run(session: Session) -> None:
    """Link every unresolved bookmaker match to a canonical match."""
    bookmaker_matches = session.exec(
        select(BookmakerMatch).where(
            col(BookmakerMatch.match_id).is_(None),
            BookmakerMatch.matching_attempts < MAX_MATCHING_ATTEMPTS,
        )
    ).all()
    for bookmaker_match in bookmaker_matches:
        match = find_match(bookmaker_match, session)
        if not match:
            # If no match found, create a new Match and link it to the BookmakerMatch
            match = create_match_from_bookmaker_match(bookmaker_match)
            session.add(match)
            session.flush()  # flush to get the new match ID

        bookmaker_match.match_id = match.id
        # Increment matching attempts
        bookmaker_match.matching_attempts += 1
        session.add(bookmaker_match)

        session.commit()


if __name__ == "__main__":
    from config import engine

    with Session(engine) as session:
        run(session)
