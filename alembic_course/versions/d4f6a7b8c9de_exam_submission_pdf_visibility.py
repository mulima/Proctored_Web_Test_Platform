"""Add per-exam student submission PDF visibility."""

from alembic import op
import sqlalchemy as sa


revision = "d4f6a7b8c9de"
down_revision = "c0175e01"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "exams",
        sa.Column(
            "show_submission_pdf",
            sa.Boolean(),
            nullable=False,
            server_default=sa.true(),
        ),
    )
    # Plain op.alter_column() issues a raw ALTER COLUMN, which SQLite doesn't support -
    # batch mode rebuilds the table instead, same pattern as every other migration here.
    with op.batch_alter_table("exams", schema=None) as batch_op:
        batch_op.alter_column("show_submission_pdf", server_default=None)


def downgrade() -> None:
    op.drop_column("exams", "show_submission_pdf")
