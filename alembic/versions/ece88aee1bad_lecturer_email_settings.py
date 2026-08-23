"""lecturer email settings

Revision ID: ece88aee1bad
Revises: 4b0eca30f2dd
Create Date: 2026-08-22 13:00:00.000000
"""
from alembic import op
import sqlalchemy as sa


revision = 'ece88aee1bad'
down_revision = '4b0eca30f2dd'
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table('lecturers', schema=None) as batch_op:
        batch_op.add_column(sa.Column('mail_backend', sa.String(length=10), nullable=False, server_default=''))
        batch_op.add_column(sa.Column('mail_from', sa.String(length=255), nullable=False, server_default=''))
        batch_op.add_column(sa.Column('smtp_host', sa.String(length=255), nullable=False, server_default=''))
        batch_op.add_column(sa.Column('smtp_port', sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column('smtp_username', sa.String(length=255), nullable=False, server_default=''))
        batch_op.add_column(sa.Column('smtp_password_encrypted', sa.Text(), nullable=True))
        batch_op.add_column(sa.Column('smtp_use_tls', sa.Boolean(), nullable=False, server_default=sa.true()))
        batch_op.add_column(sa.Column('resend_api_key_encrypted', sa.Text(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('lecturers', schema=None) as batch_op:
        batch_op.drop_column('resend_api_key_encrypted')
        batch_op.drop_column('smtp_use_tls')
        batch_op.drop_column('smtp_password_encrypted')
        batch_op.drop_column('smtp_username')
        batch_op.drop_column('smtp_port')
        batch_op.drop_column('smtp_host')
        batch_op.drop_column('mail_from')
        batch_op.drop_column('mail_backend')
