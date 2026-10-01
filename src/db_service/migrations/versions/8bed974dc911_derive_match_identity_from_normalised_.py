"""derive match identity from normalised team keys

Adds the explicit home/away sides a bookmaker reports, and replaces the
canonical fixture's label-based identity with a normalised team-key pair.

The new columns are NOT NULL, so existing rows are backfilled before the
constraints go on: bookmaker sides are recovered from the " vs " label both
scrapers build, and canonical keys from the teams already stored. Rows that the
old fuzzy matcher left holding the same identity at the same kick-off collapse
to one here, which is what makes the new unique constraint satisfiable. Rows
that merely look compatible are left alone and reported — see
`_collapse_identical_fixtures` for why that line is drawn where it is.

Revision ID: 8bed974dc911
Revises: 7953ea54e96c
Create Date: 2026-09-11 14:33:54.967412

"""

import logging
from collections.abc import Sequence
from datetime import timedelta

import sqlalchemy as sa
import sqlmodel
from alembic import op
from core.matching import same_team, team_key

# revision identifiers, used by Alembic.
revision: str = "8bed974dc911"
down_revision: str | Sequence[str] | None = "7953ea54e96c"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

logger = logging.getLogger("alembic.runtime.migration")

# Mirrors BettingRepository.KICKOFF_TOLERANCE_MINUTES at the time of writing.
# Spelt out rather than imported: a migration is a frozen snapshot of one
# moment's rules, so it must not move when the resolver's tolerance does.
_KICKOFF_TOLERANCE = timedelta(minutes=15)

# Enough of `match` to read with types applied. Going through sa.text() instead
# would hand back match_datetime as a string on SQLite, silently breaking every
# window comparison below.
_MATCH = sa.Table(
    "match",
    sa.MetaData(),
    sa.Column("id", sa.Integer()),
    sa.Column("match_label", sa.String()),
    sa.Column("match_datetime", sa.DateTime()),
    sa.Column("team1", sa.String()),
    sa.Column("team2", sa.String()),
    sa.Column("home_key", sa.String()),
    sa.Column("away_key", sa.String()),
)

# The initial schema's UNIQUE(match_label, match_datetime) is unnamed, and batch
# mode reflects it straight into the rebuilt table unless it is dropped by name.
# SQLite reflects the name as None, so batch mode is handed a convention that
# names it during the rebuild; Postgres reports the real generated name.
_STALE_UNIQUE_COLUMNS = {"match_label", "match_datetime"}
_STALE_UNIQUE_NAME = "uq_match_match_label"
_UQ_NAMING = {"uq": "uq_%(table_name)s_%(column_0_name)s"}


def _backfill_bookmaker_sides(conn) -> None:
    """Recover home/away from the label both scrapers build as '<home> vs <away>'."""
    rows = conn.execute(
        sa.text("SELECT id, match_label FROM bookmakermatch")
    ).fetchall()
    for row_id, label in rows:
        home, separator, away = (label or "").partition(" vs ")
        conn.execute(
            sa.text(
                "UPDATE bookmakermatch SET team1 = :home, team2 = :away WHERE id = :id"
            ),
            {
                "home": home or label or "",
                "away": away if separator else "",
                "id": row_id,
            },
        )


def _backfill_match_keys(conn) -> None:
    rows = conn.execute(sa.text("SELECT id, team1, team2 FROM match")).fetchall()
    for row_id, team1, team2 in rows:
        conn.execute(
            sa.text(
                "UPDATE match SET home_key = :home, away_key = :away WHERE id = :id"
            ),
            {
                "home": team_key(team1 or ""),
                "away": team_key(team2 or ""),
                "id": row_id,
            },
        )


def _fold(conn, keep_id: int, drop_id: int) -> None:
    conn.execute(
        sa.text("UPDATE bookmakermatch SET match_id = :keep WHERE match_id = :drop"),
        {"keep": keep_id, "drop": drop_id},
    )
    conn.execute(sa.text("DELETE FROM match WHERE id = :drop"), {"drop": drop_id})


def _collapse_identical_fixtures(conn) -> None:
    """Fold canonical rows that hold the same identity at the same kick-off.

    Only an exact identity collapses. Two rows whose keys merely contain one
    another ("grasshopper" and "grasshopper zurich") may be two fragments of one
    fixture or two real fixtures, and containment cannot tell those apart — it
    is the same rule that makes "Inter Mailand vs AS Rom" and "AC Mailand vs
    Lazio Rom" mutually compatible. That is exactly why the resolver refuses to
    choose, and this migration has no more evidence than the resolver does, so
    it refuses too: see `_report_ambiguous_fixtures`.

    Equal keys need no such judgement, since one team pair cannot play twice at
    one kick-off. They are also precisely the rows the new unique constraint
    rejects, so folding them is all it takes to make it satisfiable.
    """
    rows = conn.execute(
        sa.select(
            _MATCH.c.id, _MATCH.c.match_datetime, _MATCH.c.home_key, _MATCH.c.away_key
        ).order_by(_MATCH.c.match_datetime, _MATCH.c.id)
    ).fetchall()

    by_identity: dict[tuple[str, str], list[tuple[int, object]]] = {}
    for row_id, kickoff, home_key, away_key in rows:
        by_identity.setdefault((home_key, away_key), []).append((row_id, kickoff))

    for fixtures in by_identity.values():
        keep_id, keep_kickoff = fixtures[0]
        for row_id, kickoff in fixtures[1:]:
            # Beyond the window this is the same team pair meeting again — a
            # replay, a second leg — rather than one fixture written twice.
            if abs(kickoff - keep_kickoff) > _KICKOFF_TOLERANCE:
                keep_id, keep_kickoff = row_id, kickoff
                continue
            logger.info(f"collapsing duplicate match {row_id} into {keep_id}")
            _fold(conn, keep_id, row_id)


def _report_ambiguous_fixtures(conn) -> None:
    """Name the rows this migration declines to judge, so they can be repaired.

    Left alone they are not a schema problem — their keys differ, so the unique
    constraint is satisfied — but every future post of the fixture resolves to
    more than one candidate and is left unlinked. `merge_matches` is the repair;
    re-posting then relinks the rows waiting on it.
    """
    rows = conn.execute(
        sa.select(
            _MATCH.c.id,
            _MATCH.c.match_datetime,
            _MATCH.c.match_label,
            _MATCH.c.team1,
            _MATCH.c.team2,
        ).order_by(_MATCH.c.match_datetime, _MATCH.c.id)
    ).fetchall()

    # Rows are ordered by kick-off, so each one only has to be compared with
    # those that follow it until the window closes.
    for i, row in enumerate(rows):
        compatible = []
        for other in rows[i + 1 :]:
            if other[1] - row[1] > _KICKOFF_TOLERANCE:
                break
            if same_team(row[3], other[3]) and same_team(row[4], other[4]):
                compatible.append(other)
        if compatible:
            logger.warning(
                f"match {row[0]} ({row[2]!r} at {row[1]}) is indistinguishable "
                f"from {[(o[0], o[2]) for o in compatible]}; leaving both in "
                "place — merge them deliberately if they are one fixture"
            )


def _stale_match_unique(conn) -> str | None:
    """The name to drop the label-based unique by, or None if it is already gone."""
    for uq in sa.inspect(conn).get_unique_constraints("match"):
        if set(uq["column_names"]) == _STALE_UNIQUE_COLUMNS:
            return uq["name"] or _STALE_UNIQUE_NAME
    return None


def upgrade() -> None:
    """Upgrade schema."""
    conn = op.get_bind()

    # Add nullable first so existing rows survive long enough to be backfilled.
    with op.batch_alter_table("bookmakermatch", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("team1", sqlmodel.sql.sqltypes.AutoString(), nullable=True)
        )
        batch_op.add_column(
            sa.Column("team2", sqlmodel.sql.sqltypes.AutoString(), nullable=True)
        )
    with op.batch_alter_table("match", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("home_key", sqlmodel.sql.sqltypes.AutoString(), nullable=True)
        )
        batch_op.add_column(
            sa.Column("away_key", sqlmodel.sql.sqltypes.AutoString(), nullable=True)
        )

    _backfill_bookmaker_sides(conn)
    _backfill_match_keys(conn)
    _collapse_identical_fixtures(conn)
    _report_ambiguous_fixtures(conn)

    with op.batch_alter_table("bookmakermatch", schema=None) as batch_op:
        batch_op.alter_column("team1", nullable=False)
        batch_op.alter_column("team2", nullable=False)
        batch_op.drop_index(batch_op.f("ix_bookmakermatch_matching_attempts"))
        batch_op.drop_column("matching_attempts")

    # Reflected before the block: it needs the schema as it is on disk now.
    stale_unique = _stale_match_unique(conn)
    with op.batch_alter_table(
        "match", schema=None, naming_convention=_UQ_NAMING
    ) as batch_op:
        if stale_unique is not None:
            batch_op.drop_constraint(stale_unique, type_="unique")
        batch_op.alter_column("home_key", nullable=False)
        batch_op.alter_column("away_key", nullable=False)
        batch_op.create_unique_constraint(
            "uq_match_kickoff_teams", ["match_datetime", "home_key", "away_key"]
        )


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table("match", schema=None) as batch_op:
        batch_op.drop_constraint("uq_match_kickoff_teams", type_="unique")
        # Restored named, where the initial schema left it unnamed: batch mode
        # cannot add an anonymous constraint. `_stale_match_unique` matches on
        # columns, so a re-upgrade still finds and drops it.
        batch_op.create_unique_constraint(
            _STALE_UNIQUE_NAME, ["match_label", "match_datetime"]
        )
        batch_op.drop_column("away_key")
        batch_op.drop_column("home_key")

    with op.batch_alter_table("bookmakermatch", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                "matching_attempts", sa.Integer(), nullable=False, server_default="0"
            )
        )
        batch_op.create_index(
            batch_op.f("ix_bookmakermatch_matching_attempts"), ["matching_attempts"]
        )
        batch_op.drop_column("team2")
        batch_op.drop_column("team1")
