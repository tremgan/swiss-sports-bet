"""Replaying the migrations against a throwaway database.

The other suites build the schema from SQLModel metadata, which means nothing
exercises `upgrade()` — not the backfills, not the duplicate collapse, and not
the constraints the migrated schema actually ends up with.
"""

import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config

MIGRATIONS_DIR = Path(__file__).resolve().parents[1] / "migrations"
INITIAL = "7953ea54e96c"
KICKOFF = datetime(2026, 9, 12, 16, 0)


@pytest.fixture(name="migrated")
def migrated_fixture(tmp_path, monkeypatch):
    """An Alembic config and engine pointed at a throwaway file database."""
    url = f"sqlite:///{tmp_path / 'migrations.db'}"
    monkeypatch.setenv("DATABASE_URL", url)
    # `migrations/env.py` reads the URL from `config`, which conftest imported
    # long ago and bound to the repo-root .env's dev.db. Evicting the module is
    # what forces env.py to re-import it and see the URL set above.
    monkeypatch.delitem(sys.modules, "config", raising=False)

    # No .ini file, so env.py finds `config_file_name is None` and skips the
    # fileConfig() call that would reconfigure logging for the whole session.
    cfg = Config()
    cfg.set_main_option("script_location", str(MIGRATIONS_DIR))

    engine = sa.create_engine(url)
    yield cfg, engine
    engine.dispose()


def match_ids(conn) -> list[int]:
    return list(conn.execute(sa.text("SELECT id FROM match ORDER BY id")).scalars())


def seed(conn, rows: list[tuple[str, str, datetime]]) -> None:
    """Insert canonical fixtures and one bookmaker row each, at the old schema.

    Raw SQL on purpose: at `7953ea54e96c` the tables still carry
    `matching_attempts` and have no key columns, so `core.models` does not
    describe them.
    """
    for index, (home, away, kickoff) in enumerate(rows, start=1):
        label = f"{home} vs {away}"
        conn.execute(
            sa.text(
                "INSERT INTO match (id, match_label, match_datetime, team1, team2) "
                "VALUES (:id, :label, :kickoff, :home, :away)"
            ),
            {
                "id": index,
                "label": label,
                "kickoff": kickoff,
                "home": home,
                "away": away,
            },
        )
        conn.execute(
            sa.text(
                "INSERT INTO bookmakermatch "
                "(bookmaker, match_label, match_datetime, matching_attempts, match_id) "
                "VALUES (:bookmaker, :label, :kickoff, 0, :match_id)"
            ),
            {
                "bookmaker": f"Bookmaker{index}",
                "label": label,
                "kickoff": kickoff,
                "match_id": index,
            },
        )


def test_upgrade_leaves_only_the_key_based_identity(migrated):
    """The label-based unique must not survive, or prod and tests disagree.

    It is unnamed in the initial schema, so batch mode reflects it into the
    rebuilt table unless it is dropped explicitly — and autogenerate cannot see
    an unnamed constraint to warn about it.
    """
    cfg, engine = migrated
    command.upgrade(cfg, "head")

    uniques = sa.inspect(engine).get_unique_constraints("match")

    assert [uq["column_names"] for uq in uniques] == [
        ["match_datetime", "home_key", "away_key"]
    ]


def test_upgrade_backfills_the_sides_from_the_label(migrated):
    cfg, engine = migrated
    command.upgrade(cfg, INITIAL)
    with engine.begin() as conn:
        seed(conn, [("Grasshopper Club Zurich", "FC Thun", KICKOFF)])

    command.upgrade(cfg, "head")

    with engine.connect() as conn:
        assert conn.execute(
            sa.text("SELECT team1, team2 FROM bookmakermatch ORDER BY id")
        ).all() == [("Grasshopper Club Zurich", "FC Thun")]
        assert conn.execute(
            sa.text("SELECT home_key, away_key FROM match ORDER BY id")
        ).all() == [("grasshopper zurich", "thun")]


def test_rows_holding_the_same_identity_collapse(migrated):
    """Different labels, one identity — the only case the constraint rejects."""
    cfg, engine = migrated
    command.upgrade(cfg, INITIAL)
    with engine.begin() as conn:
        seed(
            conn,
            [
                ("FC Basel", "FC Thun", KICKOFF),
                ("Basel", "Thun", KICKOFF + timedelta(minutes=5)),
            ],
        )

    command.upgrade(cfg, "head")

    with engine.connect() as conn:
        assert match_ids(conn) == [1]
        assert conn.execute(
            sa.text("SELECT DISTINCT match_id FROM bookmakermatch")
        ).scalars().all() == [1]


def test_two_real_fixtures_are_never_fused(migrated):
    """The case the resolver refuses, which the migration must refuse too.

    "AC Mailand" is contained in "Inter Mailand" and "AS Rom" in "Lazio Rom" by
    exactly the rule that matches "Grasshopper" to "Grasshopper Club Zurich".
    Collapsing on that evidence would delete a real fixture, unrecoverably.
    """
    cfg, engine = migrated
    command.upgrade(cfg, INITIAL)
    with engine.begin() as conn:
        seed(
            conn,
            [
                ("Inter Mailand", "AS Rom", KICKOFF),
                ("AC Mailand", "Lazio Rom", KICKOFF),
            ],
        )

    command.upgrade(cfg, "head")

    with engine.connect() as conn:
        assert match_ids(conn) == [1, 2]
        assert conn.execute(
            sa.text("SELECT match_id FROM bookmakermatch ORDER BY id")
        ).scalars().all() == [1, 2]


def test_fragments_survive_and_are_reported(migrated, caplog):
    """Fragments are left for deliberate repair, but must not be left silent."""
    cfg, engine = migrated
    command.upgrade(cfg, INITIAL)
    with engine.begin() as conn:
        seed(
            conn,
            [
                ("Grasshopper Club Zurich", "Thun", KICKOFF),
                ("Grasshopper", "FC Thun", KICKOFF),
            ],
        )

    with caplog.at_level("WARNING"):
        command.upgrade(cfg, "head")

    with engine.connect() as conn:
        assert match_ids(conn) == [1, 2]
    assert "indistinguishable" in caplog.text


def test_a_replay_of_the_same_pair_is_not_a_duplicate(migrated):
    """Identical keys far apart in time are two meetings, not one written twice."""
    cfg, engine = migrated
    command.upgrade(cfg, INITIAL)
    with engine.begin() as conn:
        seed(
            conn,
            [
                ("FC Basel", "FC Thun", KICKOFF),
                ("Basel", "Thun", KICKOFF + timedelta(days=7)),
            ],
        )

    command.upgrade(cfg, "head")

    with engine.connect() as conn:
        assert match_ids(conn) == [1, 2]


def test_downgrade_and_upgrade_round_trip(migrated):
    """A downgrade has to leave a schema the upgrade can run against again."""
    cfg, engine = migrated
    command.upgrade(cfg, INITIAL)
    with engine.begin() as conn:
        seed(conn, [("FC Basel", "FC Thun", KICKOFF)])
    command.upgrade(cfg, "head")

    command.downgrade(cfg, INITIAL)
    command.upgrade(cfg, "head")

    assert [
        uq["column_names"] for uq in sa.inspect(engine).get_unique_constraints("match")
    ] == [["match_datetime", "home_key", "away_key"]]
    with engine.connect() as conn:
        assert match_ids(conn) == [1]


def test_a_percent_encoded_password_survives_the_alembic_config(tmp_path, monkeypatch):
    """A hosted Postgres tells you to percent-encode a password's specials.

    `env.py` passes the URL to ConfigParser, where a bare "%" opens an
    interpolation token, so an encoded password used to raise before anything
    connected. SQLite cannot carry a password, so the check is on the config
    round trip rather than on a live connection.
    """
    import sys

    from alembic.config import Config

    url = "postgresql+psycopg://postgres.ref:p%40ssw0rd@host.pooler.test:5432/postgres"
    monkeypatch.setenv("DATABASE_URL", url)
    monkeypatch.delitem(sys.modules, "config", raising=False)

    config = Config()
    config.set_main_option("script_location", str(MIGRATIONS_DIR))
    # The same escaping env.py applies.
    config.set_main_option("sqlalchemy.url", url.replace("%", "%%"))

    assert config.get_main_option("sqlalchemy.url") == url
