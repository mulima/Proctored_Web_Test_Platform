"""lecturer email verification

Revision ID: 9983bf32aa73
Revises: ece88aee1bad
Create Date: 2026-08-22 14:00:00.000000
"""
from alembic import op
import sqlalchemy as sa


revision = '9983bf32aa73'
down_revision = 'ece88aee1bad'
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table('lecturers', schema=None) as batch_op:
        batch_op.add_column(sa.Column('is_verified', sa.Boolean(), nullable=False, server_default=sa.false()))
        batch_op.add_column(sa.Column('verified_at', sa.DateTime(), nullable=True))
        batch_op.add_column(sa.Column('verification_sent_at', sa.DateTime(), nullable=True))
        batch_op.add_column(sa.Column('verification_token', sa.String(length=255), nullable=True))
        batch_op.create_index(
            batch_op.f('ix_lecturers_verification_token'), ['verification_token'], unique=False
        )


def downgrade() -> None:
    with op.batch_alter_table('lecturers', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_lecturers_verification_token'))
        batch_op.drop_column('verification_token')
        batch_op.drop_column('verification_sent_at')
        batch_op.drop_column('verified_at')
        batch_op.drop_column('is_verified')
