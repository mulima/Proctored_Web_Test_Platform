"""Add scheduled-start columns to course exams.

Revision ID: e8c1b6d4a2f0
Revises: 9c2d7f4a1e3b
Create Date: 2026-09-20
"""

from alembic import op
import sqlalchemy as sa


revision = "e8c1b6d4a2f0"
down_revision = "9c2d7f4a1e3b"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("exams", sa.Column("scheduled_start_at", sa.DateTime(), nullable=True))
    op.add_column(
        "exams",
        sa.Column(
            "scheduled_start_timezone",
            sa.String(length=64),
            nullable=False,
            server_default="UTC",
        ),
    )
    with op.batch_alter_table("exams", schema=None) as batch_op:
        batch_op.alter_column("scheduled_start_timezone", server_default=None)


def downgrade() -> None:
    op.drop_column("exams", "scheduled_start_timezone")
    op.drop_column("exams", "scheduled_start_at")
