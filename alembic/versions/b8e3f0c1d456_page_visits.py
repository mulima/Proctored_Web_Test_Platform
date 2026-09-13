"""add page_visits table

Backs the /administrator "site visitations" analytics view - one row per page
request that survives main.py's noise filter (no static assets, health checks,
in-exam polling, or the /administrator pages themselves).

Revision ID: b8e3f0c1d456
Revises: 71d5476d1c90
Create Date: 2026-09-02 09:00:00.000000
"""
from alembic import op
import sqlalchemy as sa


revision = 'b8e3f0c1d456'
down_revision = '71d5476d1c90'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'page_visits',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('at', sa.DateTime(), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
        sa.Column('path', sa.String(length=500), nullable=False, server_default=''),
        sa.Column('course_slug', sa.String(length=60), nullable=True),
        sa.Column('method', sa.String(length=10), nullable=False, server_default='GET'),
        sa.Column('status_code', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('ip', sa.String(length=64), nullable=False, server_default=''),
        sa.Column('user_agent', sa.String(length=400), nullable=False, server_default=''),
        sa.PrimaryKeyConstraint('id'),
    )
    with op.batch_alter_table('page_visits', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_page_visits_at'), ['at'], unique=False)
        batch_op.create_index(batch_op.f('ix_page_visits_course_slug'), ['course_slug'], unique=False)


def downgrade() -> None:
    with op.batch_alter_table('page_visits', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_page_visits_course_slug'))
        batch_op.drop_index(batch_op.f('ix_page_visits_at'))
    op.drop_table('page_visits')
