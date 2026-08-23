"""platform schema: lecturers and platform_logs

Revision ID: 4b0eca30f2dd
Revises:
Create Date: 2026-08-22 10:00:00.000000
"""
from alembic import op
import sqlalchemy as sa


revision = '4b0eca30f2dd'
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table('lecturers',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('email', sa.String(length=255), nullable=False),
    sa.Column('password_hash', sa.String(length=255), nullable=False),
    sa.Column('slug', sa.String(length=60), nullable=False),
    sa.Column('course_code', sa.String(length=50), nullable=False),
    sa.Column('course_title', sa.String(length=200), nullable=False),
    sa.Column('institution', sa.String(length=200), nullable=False),
    sa.Column('database_url_encrypted', sa.Text(), nullable=True),
    sa.Column('database_ready', sa.Boolean(), nullable=False),
    sa.Column('created_at', sa.DateTime(), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
    sa.Column('password_set_at', sa.DateTime(), nullable=True),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('lecturers', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_lecturers_email'), ['email'], unique=True)
        batch_op.create_index(batch_op.f('ix_lecturers_slug'), ['slug'], unique=True)

    op.create_table('platform_logs',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('at', sa.DateTime(), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
    sa.Column('level', sa.String(length=10), nullable=False),
    sa.Column('event_type', sa.String(length=60), nullable=False),
    sa.Column('message', sa.Text(), nullable=False),
    sa.Column('lecturer_id', sa.Integer(), nullable=True),
    sa.Column('ip', sa.String(length=64), nullable=False),
    sa.Column('user_agent', sa.String(length=400), nullable=False),
    sa.Column('payload', sa.JSON(), nullable=True),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('platform_logs', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_platform_logs_at'), ['at'], unique=False)
        batch_op.create_index(batch_op.f('ix_platform_logs_event_type'), ['event_type'], unique=False)
        batch_op.create_index(batch_op.f('ix_platform_logs_lecturer_id'), ['lecturer_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_platform_logs_level'), ['level'], unique=False)


def downgrade() -> None:
    with op.batch_alter_table('platform_logs', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_platform_logs_level'))
        batch_op.drop_index(batch_op.f('ix_platform_logs_lecturer_id'))
        batch_op.drop_index(batch_op.f('ix_platform_logs_event_type'))
        batch_op.drop_index(batch_op.f('ix_platform_logs_at'))

    op.drop_table('platform_logs')
    with op.batch_alter_table('lecturers', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_lecturers_slug'))
        batch_op.drop_index(batch_op.f('ix_lecturers_email'))

    op.drop_table('lecturers')
