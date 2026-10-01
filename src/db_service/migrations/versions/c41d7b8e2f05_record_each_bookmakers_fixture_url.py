"""record each bookmaker's fixture url

Adds the link back to the page a price was read from, so the published report
can send a reader straight to the bet rather than to a bookmaker's front door.

Nullable with no backfill: neither feed carries a link, so every scraper
recovers one by its own means and some fixtures will never have one. Rows
written before this ran keep NULL until their next scrape re-posts them.

Revision ID: c41d7b8e2f05
Revises: 8bed974dc911
Create Date: 2026-10-01 14:35:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
import sqlmodel
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c41d7b8e2f05"
down_revision: str | Sequence[str] | None = "8bed974dc911"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table("bookmakermatch") as batch_op:
        batch_op.add_column(
            sa.Column("url", sqlmodel.sql.sqltypes.AutoString(), nullable=True)
        )


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table("bookmakermatch") as batch_op:
        batch_op.drop_column("url")
