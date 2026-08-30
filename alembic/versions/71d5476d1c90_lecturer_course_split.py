"""split lecturer into account (lecturer) + course, one-to-many

A lecturer account and the course it runs were one row. This splits them so one
account can own many courses: creates `courses`, copies every existing lecturer
row's course-specific columns into a new courses row linked back by lecturer_id,
then drops those columns from `lecturers`. No course loses any data - it just moves
to its own row.

Revision ID: 71d5476d1c90
Revises: a4f7e2c1b123
Create Date: 2026-08-27 09:00:00.000000
"""
from alembic import op
import sqlalchemy as sa


revision = '71d5476d1c90'
down_revision = 'a4f7e2c1b123'
branch_labels = None
depends_on = None

_MOVED_COLUMNS = [
    "slug", "course_code", "course_title", "institution",
    "course_storage_mode", "database_url_encrypted", "platform_db_schema", "database_ready",
    "mail_backend", "mail_from", "smtp_host", "smtp_port", "smtp_username",
    "smtp_password_encrypted", "smtp_use_tls", "resend_api_key_encrypted",
]


def upgrade() -> None:
    op.create_table(
        'courses',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('lecturer_id', sa.Integer(), nullable=False),
        sa.Column('slug', sa.String(length=60), nullable=False),
        sa.Column('course_code', sa.String(length=50), nullable=False, server_default=''),
        sa.Column('course_title', sa.String(length=200), nullable=False, server_default=''),
        sa.Column('institution', sa.String(length=200), nullable=False, server_default=''),
        sa.Column('course_storage_mode', sa.String(length=20), nullable=False, server_default='external'),
        sa.Column('database_url_encrypted', sa.Text(), nullable=True),
        sa.Column('platform_db_schema', sa.String(length=100), nullable=True),
        sa.Column('database_ready', sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column('mail_backend', sa.String(length=10), nullable=False, server_default=''),
        sa.Column('mail_from', sa.String(length=255), nullable=False, server_default=''),
        sa.Column('smtp_host', sa.String(length=255), nullable=False, server_default=''),
        sa.Column('smtp_port', sa.Integer(), nullable=True),
        sa.Column('smtp_username', sa.String(length=255), nullable=False, server_default=''),
        sa.Column('smtp_password_encrypted', sa.Text(), nullable=True),
        sa.Column('smtp_use_tls', sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column('resend_api_key_encrypted', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
        sa.ForeignKeyConstraint(['lecturer_id'], ['lecturers.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
    )
    with op.batch_alter_table('courses', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_courses_lecturer_id'), ['lecturer_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_courses_slug'), ['slug'], unique=True)

    # Every existing lecturer row already IS a course, one-for-one. Copy its
    # course-specific columns into a new courses row before they're dropped below.
    op.execute(
        """
        INSERT INTO courses (
            lecturer_id, slug, course_code, course_title, institution,
            course_storage_mode, database_url_encrypted, platform_db_schema, database_ready,
            mail_backend, mail_from, smtp_host, smtp_port, smtp_username,
            smtp_password_encrypted, smtp_use_tls, resend_api_key_encrypted, created_at
        )
        SELECT
            id, slug, course_code, course_title, institution,
            course_storage_mode, database_url_encrypted, platform_db_schema, database_ready,
            mail_backend, mail_from, smtp_host, smtp_port, smtp_username,
            smtp_password_encrypted, smtp_use_tls, resend_api_key_encrypted, created_at
        FROM lecturers
        """
    )

    with op.batch_alter_table('lecturers', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_lecturers_slug'))
        for column in _MOVED_COLUMNS:
            batch_op.drop_column(column)


def downgrade() -> None:
    with op.batch_alter_table('lecturers', schema=None) as batch_op:
        batch_op.add_column(sa.Column('slug', sa.String(length=60), nullable=True))
        batch_op.add_column(sa.Column('course_code', sa.String(length=50), nullable=False, server_default=''))
        batch_op.add_column(sa.Column('course_title', sa.String(length=200), nullable=False, server_default=''))
        batch_op.add_column(sa.Column('institution', sa.String(length=200), nullable=False, server_default=''))
        batch_op.add_column(sa.Column('course_storage_mode', sa.String(length=20), nullable=False, server_default='external'))
        batch_op.add_column(sa.Column('database_url_encrypted', sa.Text(), nullable=True))
        batch_op.add_column(sa.Column('platform_db_schema', sa.String(length=100), nullable=True))
        batch_op.add_column(sa.Column('database_ready', sa.Boolean(), nullable=False, server_default=sa.false()))
        batch_op.add_column(sa.Column('mail_backend', sa.String(length=10), nullable=False, server_default=''))
        batch_op.add_column(sa.Column('mail_from', sa.String(length=255), nullable=False, server_default=''))
        batch_op.add_column(sa.Column('smtp_host', sa.String(length=255), nullable=False, server_default=''))
        batch_op.add_column(sa.Column('smtp_port', sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column('smtp_username', sa.String(length=255), nullable=False, server_default=''))
        batch_op.add_column(sa.Column('smtp_password_encrypted', sa.Text(), nullable=True))
        batch_op.add_column(sa.Column('smtp_use_tls', sa.Boolean(), nullable=False, server_default=sa.true()))
        batch_op.add_column(sa.Column('resend_api_key_encrypted', sa.Text(), nullable=True))

    # Only the first course per lecturer can round-trip back onto the single-course
    # shape; a downgrade after a lecturer has created a second course is lossy by
    # nature of the schema it's returning to, so this takes the earliest one only.
    op.execute(
        """
        UPDATE lecturers SET
            slug = c.slug, course_code = c.course_code, course_title = c.course_title,
            institution = c.institution, course_storage_mode = c.course_storage_mode,
            database_url_encrypted = c.database_url_encrypted, platform_db_schema = c.platform_db_schema,
            database_ready = c.database_ready, mail_backend = c.mail_backend, mail_from = c.mail_from,
            smtp_host = c.smtp_host, smtp_port = c.smtp_port, smtp_username = c.smtp_username,
            smtp_password_encrypted = c.smtp_password_encrypted, smtp_use_tls = c.smtp_use_tls,
            resend_api_key_encrypted = c.resend_api_key_encrypted
        FROM (
            SELECT * FROM (
                SELECT courses.*, ROW_NUMBER() OVER (PARTITION BY lecturer_id ORDER BY id) AS rn
                FROM courses
            ) ranked WHERE rn = 1
        ) c
        WHERE lecturers.id = c.lecturer_id
        """
    )

    op.drop_table('courses')

    with op.batch_alter_table('lecturers', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_lecturers_slug'), ['slug'], unique=True)
