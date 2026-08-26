"""add odds last seen timestamp

Revision ID: 20260826_181500
Revises: 20260809_164500
Create Date: 2026-08-26 18:15:00
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect


revision = "20260826_181500"
down_revision = "20260809_164500"
branch_labels = None
depends_on = None


def _has_column(bind, table_name: str, column_name: str) -> bool:
    return any(
        column.get("name") == column_name
        for column in inspect(bind).get_columns(table_name)
    )


def upgrade() -> None:
    bind = op.get_bind()
    if "odds" not in set(inspect(bind).get_table_names()):
        return
    if not _has_column(bind, "odds", "last_seen_at"):
        op.add_column(
            "odds",
            sa.Column("last_seen_at", sa.DateTime(), nullable=True),
        )
    op.execute(
        sa.text(
            "UPDATE odds "
            "SET last_seen_at = COALESCE(last_seen_at, valid_from, created_at) "
            "WHERE last_seen_at IS NULL"
        )
    )
    op.alter_column("odds", "last_seen_at", nullable=False)
    op.execute(
        sa.text(
            "CREATE INDEX IF NOT EXISTS ix_odds_last_seen_at "
            "ON odds (last_seen_at)"
        )
    )


def downgrade() -> None:
    bind = op.get_bind()
    if not _has_column(bind, "odds", "last_seen_at"):
        return
    op.execute(sa.text("DROP INDEX IF EXISTS ix_odds_last_seen_at"))
    op.drop_column("odds", "last_seen_at")
