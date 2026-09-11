"""SQLModel schema shared by every service.

Three tables model the pipeline: a canonical :class:`Match` (one real-world
fixture), the :class:`BookmakerMatch` rows each bookmaker contributes for it,
and the :class:`SportsBettingOdds` snapshot written on every scrape run.

Arbitrage math lives in :mod:`core.arbitrage` and operates on plain mappings,
so importing this module does not pull in a numerics stack.
"""

from datetime import UTC, datetime

from pydantic import model_validator
from sqlmodel import Field, Relationship, SQLModel, UniqueConstraint

# Odds outside this range are certainly a parsing error rather than a real price.
MIN_ODDS = 1.01
MAX_ODDS = 50.0


class MatchBase(SQLModel):
    """A canonical football fixture, independent of any bookmaker's naming."""

    match_label: str
    match_datetime: datetime
    team1: str
    team2: str


class Match(MatchBase, table=True):
    __table_args__ = (UniqueConstraint("match_label", "match_datetime"),)

    id: int | None = Field(default=None, primary_key=True)

    bookmaker_matches: list["BookmakerMatch"] = Relationship(back_populates="match")

    def __repr__(self) -> str:
        return (
            f"<Match(id={self.id}, label={self.match_label!r}, "
            f"datetime={self.match_datetime!r})>"
        )


class MatchCreate(MatchBase):
    pass


class BookmakerMatchBase(SQLModel):
    bookmaker: str
    match_label: str
    match_datetime: datetime


class BookmakerMatch(BookmakerMatchBase, table=True):
    """One bookmaker's view of a fixture, before it is linked to a `Match`."""

    __table_args__ = (UniqueConstraint("bookmaker", "match_label", "match_datetime"),)

    id: int | None = Field(default=None, primary_key=True)
    matching_attempts: int = Field(default=0, index=True)

    sports_betting_odds: list["SportsBettingOdds"] = Relationship(
        back_populates="bookmaker_match"
    )

    match_id: int | None = Field(default=None, foreign_key="match.id")
    match: Match | None = Relationship(back_populates="bookmaker_matches")


class BookmakerMatchCreate(BookmakerMatchBase):
    pass


class SportsBettingOddsBase(SQLModel):
    """A 1X2 odds snapshot. `draw_odds` is absent for two-way markets."""

    timestamp: datetime = Field(default_factory=lambda: datetime.now(UTC), index=True)
    team1_odds: float
    draw_odds: float | None = None
    team2_odds: float

    @model_validator(mode="after")
    def validate_odds(self) -> "SportsBettingOddsBase":
        for field, value in [
            ("team1_odds", self.team1_odds),
            ("team2_odds", self.team2_odds),
        ]:
            if not (MIN_ODDS <= value <= MAX_ODDS):
                raise ValueError(
                    f"{field} {value} outside plausible range [{MIN_ODDS}, {MAX_ODDS}]"
                )
        if self.draw_odds and not (MIN_ODDS <= self.draw_odds <= MAX_ODDS):
            raise ValueError(
                f"draw_odds {self.draw_odds} outside plausible range "
                f"[{MIN_ODDS}, {MAX_ODDS}]"
            )

        # A single bookmaker's book always overrounds; under 1.0 means we
        # mis-parsed the market rather than found free money.
        implied = 1 / self.team1_odds + 1 / self.team2_odds
        if self.draw_odds:
            implied += 1 / self.draw_odds
        if implied < 1.0:
            raise ValueError(
                f"implied probability {implied:.3f} is below 1.0 — odds look invalid"
            )

        return self


class SportsBettingOdds(SportsBettingOddsBase, table=True):
    id: int | None = Field(default=None, primary_key=True)

    bookmaker_match_id: int | None = Field(
        default=None, foreign_key="bookmakermatch.id"
    )
    bookmaker_match: BookmakerMatch | None = Relationship(
        back_populates="sports_betting_odds"
    )


class SportsBettingOddsCreate(SportsBettingOddsBase):
    bookmaker_match_id: int | None = None
