"""Database access for the betting tables.

Read helpers take `limit`/`offset` because every table here grows without
bound: the odds table gains a row per match, per bookmaker, per scrape run,
forever.
"""

import logging
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Any, cast

from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, col, select

from core.matching import same_team, team_key
from core.models import (
    BookmakerMatch,
    BookmakerMatchCreate,
    Match,
    SportsBettingOdds,
    SportsBettingOddsCreate,
)

logger = logging.getLogger("db_service.repositories")


class BettingRepository:
    # Odds older than this are treated as stale and ignored when comparing
    # bookmakers, so a bookmaker that stopped reporting cannot pin an old price
    # against a fresh one.
    MAX_ODDS_AGE_HOURS = 1

    def __init__(self, session: Session):
        self.session = session

    # Bookmakers occasionally disagree about kick-off by a minute or two. The
    # window can stay forgiving because an ambiguous block is refused rather
    # than guessed at.
    KICKOFF_TOLERANCE_MINUTES = 15

    def create_bookmaker_match(self, match: BookmakerMatchCreate) -> BookmakerMatch:
        """Insert a bookmaker match, linking it to its canonical fixture.

        Linking happens here rather than in a later pass, so a bookmaker match
        is never persisted in an unresolved state that something else has to
        remember to clean up.
        """
        try:
            match_obj = BookmakerMatch.model_validate(match)
            match_obj.match_id = self._resolve_match(match_obj)
            self.session.add(match_obj)
            self.session.commit()
            self.session.refresh(match_obj)
            return match_obj
        except IntegrityError:
            self.session.rollback()
            existing = self.session.exec(
                select(BookmakerMatch).where(
                    BookmakerMatch.bookmaker == match.bookmaker,
                    BookmakerMatch.match_label == match.match_label,
                    BookmakerMatch.match_datetime == match.match_datetime,
                )
            ).first()
            if existing is None:
                # The conflict was not the (bookmaker, label, datetime) unique
                # constraint we expect, so swallowing it would hide a real fault.
                raise
            if existing.match_id is None:
                # The row was persisted unlinked because its block was ambiguous
                # when it first arrived. Re-posting is the only retry there is
                # now that the batch pass is gone, so take it: once the
                # duplicate behind the ambiguity is repaired, the next scrape
                # run is what makes the fixture visible again.
                existing.match_id = self._resolve_match(existing)
                if existing.match_id is not None:
                    self.session.add(existing)
                    self.session.commit()
                    self.session.refresh(existing)
            return existing

    def _resolve_match(self, bookmaker_match: BookmakerMatch) -> int | None:
        """Find or create the canonical `Match` for a bookmaker's fixture.

        Returns None when more than one canonical fixture is plausible. Refusing
        to choose is deliberate: a wrong link is permanent and silently corrupts
        the odds comparison, whereas an unlinked row is visibly absent.
        """
        kickoff = bookmaker_match.match_datetime

        # One query, then token containment scoped to the kick-off. That scope
        # is load-bearing: across a whole feed containment merges distinct clubs
        # ("AC Mailand" into "Inter Mailand"), but a club plays at most once at
        # any given time. Blocks are small, so there is nothing to optimise.
        tolerance = timedelta(minutes=self.KICKOFF_TOLERANCE_MINUTES)
        nearby = self.session.exec(
            select(Match)
            .where(
                Match.match_datetime >= kickoff - tolerance,
                Match.match_datetime <= kickoff + tolerance,
            )
            .order_by(col(Match.id))
        ).all()
        candidates = [
            m
            for m in nearby
            if same_team(bookmaker_match.team1, m.team1)
            and same_team(bookmaker_match.team2, m.team2)
        ]

        if len(candidates) > 1:
            # Two canonical rows both look like this fixture. They may be
            # fragments of one, or two genuinely different fixtures — and
            # containment cannot tell those apart, since "Inter Mailand vs AS
            # Rom" and "AC Mailand vs Lazio Rom" are mutually compatible by
            # exactly the rule that makes "Grasshopper" match "Grasshopper Club
            # Zurich". Merging on that evidence would silently fuse two real
            # fixtures, so refuse and leave the row visibly unlinked.
            logger.warning(
                f"ambiguous fixture for {bookmaker_match.match_label!r} at "
                f"{kickoff}: {[m.match_label for m in candidates]}; "
                "leaving unlinked"
            )
            return None

        if candidates:
            return candidates[0].id

        # Read the keys into locals: losing the race below expunges `created`,
        # and the lookup that follows must not depend on a transient object.
        home_key = team_key(bookmaker_match.team1)
        away_key = team_key(bookmaker_match.team2)
        created = Match(
            match_label=f"{bookmaker_match.team1} vs {bookmaker_match.team2}",
            match_datetime=kickoff,
            team1=bookmaker_match.team1,
            team2=bookmaker_match.team2,
            home_key=home_key,
            away_key=away_key,
        )
        try:
            # A savepoint, so losing a race costs only this INSERT: the caller's
            # transaction stays usable and its bookmaker match still commits,
            # against whichever row the winner created.
            with self.session.begin_nested():
                self.session.add(created)
                self.session.flush()  # assigns the id we are about to link to
        except IntegrityError:
            existing = self.session.exec(
                select(Match).where(
                    Match.match_datetime == kickoff,
                    Match.home_key == home_key,
                    Match.away_key == away_key,
                )
            ).first()
            if existing is None:
                # Not the race the unique constraint describes, so swallowing
                # it would hide a real fault.
                raise
            logger.info(
                f"lost the race to create {created.match_label!r}; "
                f"linking to match {existing.id}"
            )
            return existing.id
        return created.id

    def merge_matches(self, keep: Match, drop: Match) -> None:
        """Fold one canonical fixture into another, re-pointing its bookmakers.

        Deliberately not called from the resolver, and not by the migration
        either: at write time two compatible canonical rows are indistinguishable
        from two real fixtures, so merging on that evidence would fuse genuine
        matches. Both refuse and report instead, and this is the deliberate
        repair they leave to a human — after which re-posting relinks the rows.
        """
        logger.info(f"merging {drop.match_label!r} into {keep.match_label!r}")
        for bm in self.session.exec(
            select(BookmakerMatch).where(BookmakerMatch.match_id == drop.id)
        ).all():
            bm.match_id = keep.id
            self.session.add(bm)

        # Flush the re-pointing first, then forget the stale collection:
        # deleting a match otherwise nulls the foreign key of every row
        # SQLAlchemy still believes belongs to it, undoing the lines above.
        self.session.flush()
        self.session.expire(drop, ["bookmaker_matches"])
        self.session.delete(drop)
        self.session.flush()

    def get_bookmaker_matches(
        self, limit: int | None = None, offset: int = 0
    ) -> Sequence[BookmakerMatch]:
        statement = (
            select(BookmakerMatch).order_by(col(BookmakerMatch.id)).offset(offset)
        )
        if limit is not None:
            statement = statement.limit(limit)
        return self.session.exec(statement).all()

    def create_odds(self, odds: SportsBettingOddsCreate) -> SportsBettingOdds:
        odds_obj = SportsBettingOdds.model_validate(odds)
        self.session.add(odds_obj)
        try:
            self.session.commit()
        except Exception:
            self.session.rollback()
            raise
        self.session.refresh(odds_obj)
        return odds_obj

    def create_odds_bulk(self, odds_list: list[SportsBettingOddsCreate]) -> int:
        odds_objs = [SportsBettingOdds.model_validate(o) for o in odds_list]
        self.session.add_all(odds_objs)
        try:
            self.session.commit()
        except Exception:
            self.session.rollback()
            raise
        return len(odds_objs)

    def get_odds(
        self, limit: int | None = None, offset: int = 0
    ) -> Sequence[SportsBettingOdds]:
        statement = (
            select(SportsBettingOdds)
            .order_by(col(SportsBettingOdds.timestamp).desc())
            .offset(offset)
        )
        if limit is not None:
            statement = statement.limit(limit)
        return self.session.exec(statement).all()

    def _latest_odds_subquery(self):
        """Rank each bookmaker match's odds newest-first; rank 1 is the live price.

        A window function keeps "latest odds per bookmaker match" in a single
        round trip, instead of one query per bookmaker match.
        """
        return select(
            col(SportsBettingOdds.id).label("odds_id"),
            func.row_number()
            .over(
                partition_by=col(SportsBettingOdds.bookmaker_match_id),
                order_by=col(SportsBettingOdds.timestamp).desc(),
            )
            .label("rank"),
        ).subquery()

    def get_matches_with_odds(
        self, limit: int | None = None, offset: int = 0
    ) -> list[dict[str, Any]]:
        """Matches currently priced by more than one bookmaker, soonest first.

        Runs as two queries regardless of result size: one to page over the
        qualifying matches, one to fetch their odds. Filtering happens in SQL so
        that `limit` applies to matches that actually qualify.
        """
        # Timestamps are stored as naive UTC, so the cutoff has to match.
        cutoff = datetime.now(UTC).replace(tzinfo=None) - timedelta(
            hours=self.MAX_ODDS_AGE_HOURS
        )

        ranked = self._latest_odds_subquery()
        qualifying = (
            select(Match.id)
            .join(BookmakerMatch, col(BookmakerMatch.match_id) == Match.id)
            .join(
                SportsBettingOdds,
                col(SportsBettingOdds.bookmaker_match_id) == BookmakerMatch.id,
            )
            .join(ranked, col(SportsBettingOdds.id) == ranked.c.odds_id)
            .where(ranked.c.rank == 1, col(SportsBettingOdds.timestamp) >= cutoff)
            .group_by(col(Match.id), col(Match.match_datetime))
            .having(func.count(func.distinct(col(BookmakerMatch.bookmaker))) > 1)
            .order_by(col(Match.match_datetime))
            .offset(offset)
        )
        if limit is not None:
            qualifying = qualifying.limit(limit)

        match_ids = list(self.session.exec(qualifying).all())
        if not match_ids:
            return []

        ranked = self._latest_odds_subquery()
        rows = self.session.exec(
            select(Match, BookmakerMatch.bookmaker, SportsBettingOdds)
            .join(BookmakerMatch, col(BookmakerMatch.match_id) == Match.id)
            .join(
                SportsBettingOdds,
                col(SportsBettingOdds.bookmaker_match_id) == BookmakerMatch.id,
            )
            .join(ranked, col(SportsBettingOdds.id) == ranked.c.odds_id)
            .where(
                col(Match.id).in_(match_ids),
                ranked.c.rank == 1,
                col(SportsBettingOdds.timestamp) >= cutoff,
            )
        ).all()

        by_match: dict[int, dict[str, Any]] = {}
        for match, bookmaker, odds in rows:
            # Rows come straight from the database, so the id is always set.
            entry = by_match.setdefault(
                cast(int, match.id), {"match": match, "bookmaker_odds": {}}
            )
            entry["bookmaker_odds"][bookmaker] = {
                "team1_odds": odds.team1_odds,
                "draw_odds": odds.draw_odds,
                "team2_odds": odds.team2_odds,
                "timestamp": odds.timestamp,
            }

        # Preserve the page's ordering from the first query.
        return [by_match[match_id] for match_id in match_ids if match_id in by_match]
