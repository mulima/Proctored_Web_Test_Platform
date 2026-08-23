"""lecturer storage modes

Revision ID: a4f7e2c1b123
Revises: 9983bf32aa73
Create Date: 2026-08-23 12:00:00.000000
"""
from alembic import op
import sqlalchemy as sa


revision = 'a4f7e2c1b123'
down_revision = '9983bf32aa73'
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table('lecturers', schema=None) as batch_op:
        batch_op.add_column(
            sa.Column('course_storage_mode', sa.String(length=20), nullable=False, server_default='external')
        )
        batch_op.add_column(sa.Column('platform_db_schema', sa.String(length=100), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('lecturers', schema=None) as batch_op:
        batch_op.drop_column('platform_db_schema')
        batch_op.drop_column('course_storage_mode')
