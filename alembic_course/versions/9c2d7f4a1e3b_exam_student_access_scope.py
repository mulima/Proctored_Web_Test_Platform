"""Add per-exam student access scope and allowlist table.

Revision ID: 9c2d7f4a1e3b
Revises: f12ab34cd567
Create Date: 2026-09-20
"""

from alembic import op
import sqlalchemy as sa


revision = "9c2d7f4a1e3b"
down_revision = "f12ab34cd567"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "exams",
        sa.Column("access_scope", sa.String(length=20), nullable=False, server_default="all"),
    )

    op.create_table(
        "exam_allowed_students",
        sa.Column("exam_id", sa.Integer(), nullable=False),
        sa.Column("student_id", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(["exam_id"], ["exams.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["student_id"], ["students.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("exam_id", "student_id"),
    )

    with op.batch_alter_table("exams", schema=None) as batch_op:
        batch_op.alter_column("access_scope", server_default=None)


def downgrade() -> None:
    op.drop_table("exam_allowed_students")
    op.drop_column("exams", "access_scope")
