"""Add submission review fields and audit event history.

Revision ID: f12ab34cd567
Revises: d4f6a7b8c9de
Create Date: 2026-08-26
"""

from alembic import op
import sqlalchemy as sa


revision = "f12ab34cd567"
down_revision = "d4f6a7b8c9de"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("attempts", sa.Column("reviewed_at", sa.DateTime(), nullable=True))
    op.add_column(
        "attempts",
        sa.Column("reviewed_by", sa.String(length=255), nullable=False, server_default=""),
    )
    op.add_column(
        "attempts",
        sa.Column("review_notes", sa.Text(), nullable=False, server_default=""),
    )
    op.add_column("attempts", sa.Column("last_pdf_audit_at", sa.DateTime(), nullable=True))
    op.add_column("attempts", sa.Column("last_pdf_audit_match", sa.Boolean(), nullable=True))
    op.add_column(
        "attempts",
        sa.Column(
            "last_pdf_audit_stored_sha256",
            sa.String(length=64),
            nullable=False,
            server_default="",
        ),
    )
    op.add_column(
        "attempts",
        sa.Column(
            "last_pdf_audit_current_sha256",
            sa.String(length=64),
            nullable=False,
            server_default="",
        ),
    )
    op.add_column(
        "attempts",
        sa.Column("last_pdf_audit_message", sa.Text(), nullable=False, server_default=""),
    )

    op.create_table(
        "submission_audit_events",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("attempt_id", sa.Integer(), nullable=False),
        sa.Column("exam_id", sa.Integer(), nullable=False),
        sa.Column("action", sa.String(length=30), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="ok"),
        sa.Column("actor", sa.String(length=255), nullable=False, server_default=""),
        sa.Column("at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("stored_pdf_sha256", sa.String(length=64), nullable=False, server_default=""),
        sa.Column("current_pdf_sha256", sa.String(length=64), nullable=False, server_default=""),
        sa.Column("is_match", sa.Boolean(), nullable=True),
        sa.Column("message", sa.Text(), nullable=False, server_default=""),
        sa.Column("payload", sa.JSON(), nullable=True),
        sa.ForeignKeyConstraint(["attempt_id"], ["attempts.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["exam_id"], ["exams.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_submission_audit_events_attempt_id", "submission_audit_events", ["attempt_id"])
    op.create_index("ix_submission_audit_events_exam_id", "submission_audit_events", ["exam_id"])
    op.create_index("ix_submission_audit_events_action", "submission_audit_events", ["action"])
    op.create_index("ix_submission_audit_events_status", "submission_audit_events", ["status"])
    op.create_index("ix_submission_audit_events_at", "submission_audit_events", ["at"])

    # Plain op.alter_column() issues a raw ALTER COLUMN, which SQLite doesn't support -
    # batch mode rebuilds the table instead, same pattern as every other migration here.
    # One batch context for all five so SQLite only rebuilds the table once.
    with op.batch_alter_table("attempts", schema=None) as batch_op:
        batch_op.alter_column("reviewed_by", server_default=None)
        batch_op.alter_column("review_notes", server_default=None)
        batch_op.alter_column("last_pdf_audit_stored_sha256", server_default=None)
        batch_op.alter_column("last_pdf_audit_current_sha256", server_default=None)
        batch_op.alter_column("last_pdf_audit_message", server_default=None)


def downgrade() -> None:
    op.drop_index("ix_submission_audit_events_at", table_name="submission_audit_events")
    op.drop_index("ix_submission_audit_events_status", table_name="submission_audit_events")
    op.drop_index("ix_submission_audit_events_action", table_name="submission_audit_events")
    op.drop_index("ix_submission_audit_events_exam_id", table_name="submission_audit_events")
    op.drop_index("ix_submission_audit_events_attempt_id", table_name="submission_audit_events")
    op.drop_table("submission_audit_events")

    op.drop_column("attempts", "last_pdf_audit_message")
    op.drop_column("attempts", "last_pdf_audit_current_sha256")
    op.drop_column("attempts", "last_pdf_audit_stored_sha256")
    op.drop_column("attempts", "last_pdf_audit_match")
    op.drop_column("attempts", "last_pdf_audit_at")
    op.drop_column("attempts", "review_notes")
    op.drop_column("attempts", "reviewed_by")
    op.drop_column("attempts", "reviewed_at")
