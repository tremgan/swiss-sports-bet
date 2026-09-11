"""Database access for the betting tables.

Read helpers take `limit`/`offset` because every table here grows without
bound: the odds table gains a row per match, per bookmaker, per scrape run,
forever.
"""

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Any, cast

from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, col, select

from core.models import (
    BookmakerMatch,
    BookmakerMatchCreate,
    Match,
    SportsBettingOdds,
    SportsBettingOddsCreate,
)


class BettingRepository:
    # Odds older than this are treated as stale and ignored when comparing
    # bookmakers, so a bookmaker that stopped reporting cannot pin an old price
    # against a fresh one.
    MAX_ODDS_AGE_HOURS = 1

    def __init__(self, session: Session):
        self.session = session

    def create_bookmaker_match(self, match: BookmakerMatchCreate) -> BookmakerMatch:
        """Insert a bookmaker match, returning the existing row if it is a repeat."""
        try:
            match_obj = BookmakerMatch.model_validate(match)
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
            return existing

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
