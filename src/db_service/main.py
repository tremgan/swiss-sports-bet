"""HTTP surface for the betting database.

The schema is managed by Alembic (`alembic upgrade head`), not created on
import, so a deploy can never silently diverge from the migration history.
"""

from collections.abc import Iterator, Sequence
from typing import Annotated, Any

from config import engine, logger
from core.models import (
    BookmakerMatch,
    BookmakerMatchCreate,
    SportsBettingOdds,
    SportsBettingOddsCreate,
)
from fastapi import Depends, FastAPI, Query, Request
from fastapi.responses import JSONResponse
from repositories import BettingRepository
from sqlalchemy.exc import SQLAlchemyError
from sqlmodel import Session

MAX_PAGE_SIZE = 200

app = FastAPI(
    title="swiss-sports-bet db_service",
    description=(
        "Stores scraped bookmaker odds and reconciles events across bookmakers."
    ),
    version="0.1.0",
)


def get_session() -> Iterator[Session]:
    if not engine:
        raise RuntimeError("Database engine is not initialised; set DATABASE_URL.")

    with Session(engine) as session:
        yield session


def get_repo(session: Annotated[Session, Depends(get_session)]) -> BettingRepository:
    return BettingRepository(session)


Repo = Annotated[BettingRepository, Depends(get_repo)]
Limit = Annotated[int | None, Query(ge=1, le=MAX_PAGE_SIZE)]
Offset = Annotated[int, Query(ge=0)]


@app.exception_handler(SQLAlchemyError)
def handle_database_error(request: Request, exc: SQLAlchemyError) -> JSONResponse:
    """Log the traceback and return a 500 rather than leaking driver internals."""
    logger.exception(f"database error handling {request.method} {request.url.path}")
    return JSONResponse(status_code=500, content={"detail": "database error"})


@app.get("/")
def root() -> dict[str, str]:
    """Health check."""
    return {"message": "All good!"}


@app.post("/bookmaker_matches/")
def create_bookmaker_match(match: BookmakerMatchCreate, repo: Repo) -> BookmakerMatch:
    """Create a bookmaker match, or return the existing row if it is a repeat."""
    return repo.create_bookmaker_match(match)


@app.get("/bookmaker_matches/")
def read_bookmaker_matches(
    repo: Repo, limit: Limit = None, offset: Offset = 0
) -> Sequence[BookmakerMatch]:
    """List bookmaker matches, oldest first."""
    return repo.get_bookmaker_matches(limit=limit, offset=offset)


@app.post("/sports_betting_odds/")
def create_sports_betting_odds(
    odds: SportsBettingOddsCreate, repo: Repo
) -> SportsBettingOdds:
    """Record a single odds snapshot."""
    return repo.create_odds(odds)


@app.post("/sports_betting_odds/bulk/")
def create_sports_betting_odds_bulk(
    odds_list: list[SportsBettingOddsCreate], repo: Repo
) -> dict[str, int]:
    """Record several odds snapshots in one transaction."""
    return {"created": repo.create_odds_bulk(odds_list)}


@app.get("/sports_betting_odds/")
def read_sports_betting_odds(
    repo: Repo, limit: Limit = None, offset: Offset = 0
) -> Sequence[SportsBettingOdds]:
    """List odds snapshots, newest first."""
    return repo.get_odds(limit=limit, offset=offset)


@app.get("/matches/with_odds/", response_model=None)
def read_matches_with_odds(
    repo: Repo, limit: Limit = None, offset: Offset = 0
) -> list[dict[str, Any]]:
    """Matches currently priced by more than one bookmaker, soonest kick-off first."""
    return repo.get_matches_with_odds(limit=limit, offset=offset)
