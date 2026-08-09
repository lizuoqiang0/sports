"""drop unused ai_config columns

Revision ID: 20260809_164500
Revises:
Create Date: 2026-08-09 16:45:00
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect


revision = "20260809_164500"
down_revision = None
branch_labels = None
depends_on = None


def _has_table(bind, table_name: str) -> bool:
    return table_name in set(inspect(bind).get_table_names())


def _has_column(bind, table_name: str, column_name: str) -> bool:
    for column in inspect(bind).get_columns(table_name):
        if column.get("name") == column_name:
            return True
    return False


def upgrade() -> None:
    bind = op.get_bind()
    if not _has_table(bind, "ai_configs"):
        return

    op.execute(
        sa.text(
            """
            UPDATE ai_configs
            SET strategy = 'simple'
            WHERE strategy IS NULL
               OR TRIM(strategy) = ''
               OR LOWER(TRIM(strategy)) <> 'simple'
            """
        )
    )

    if bind.dialect.name == "postgresql":
        op.execute(sa.text("ALTER TABLE ai_configs ALTER COLUMN strategy SET DEFAULT 'simple'"))
        op.execute(sa.text("UPDATE ai_configs SET strategy = 'simple' WHERE strategy IS NULL"))
        op.execute(sa.text("ALTER TABLE ai_configs ALTER COLUMN strategy SET NOT NULL"))
        op.execute(sa.text("ALTER TABLE ai_configs DROP CONSTRAINT IF EXISTS ck_ai_configs_strategy_high_win_rate"))
        op.execute(sa.text("ALTER TABLE ai_configs DROP COLUMN IF EXISTS auto_cashout"))
        op.execute(sa.text("ALTER TABLE ai_configs DROP COLUMN IF EXISTS cashout_threshold"))
        return

    with op.batch_alter_table("ai_configs") as batch_op:
        if _has_column(bind, "ai_configs", "auto_cashout"):
            batch_op.drop_column("auto_cashout")
        if _has_column(bind, "ai_configs", "cashout_threshold"):
            batch_op.drop_column("cashout_threshold")


def downgrade() -> None:
    bind = op.get_bind()
    if not _has_table(bind, "ai_configs"):
        return

    with op.batch_alter_table("ai_configs") as batch_op:
        if not _has_column(bind, "ai_configs", "auto_cashout"):
            batch_op.add_column(
                sa.Column(
                    "auto_cashout",
                    sa.Boolean(),
                    nullable=False,
                    server_default=sa.text("false"),
                )
            )
        if not _has_column(bind, "ai_configs", "cashout_threshold"):
            batch_op.add_column(
                sa.Column(
                    "cashout_threshold",
                    sa.Float(),
                    nullable=False,
                    server_default=sa.text("0.8"),
                )
            )
