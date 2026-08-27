"""Alembic environment for the COURSE schema.

Deliberately independent of app/config.py - this is not the platform's own
database. The connection string comes from alembic_course.ini's own
sqlalchemy.url, exactly like a plain `alembic init` scaffold, since whoever
runs this (a lecturer provisioning their own database, or this repo
generating docs/DATABASE_SCHEMA.md via --sql offline mode) is never this
app's own deployment.
"""

from logging.config import fileConfig
import os

from alembic import context
from sqlalchemy import engine_from_config, pool

from app.models_course import CourseBase

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = CourseBase.metadata


def _normalise_database_url(url: str) -> str:
    if url.startswith("postgres://"):
        return url.replace("postgres://", "postgresql+psycopg2://", 1)
    if url.startswith("postgresql://"):
        return url.replace("postgresql://", "postgresql+psycopg2://", 1)
    return url


def _resolved_url() -> str:
    # Prefer explicit env override so migrations can target the same live
    # course DB used by the lecturer setup without editing ini files.
    raw = (os.getenv("COURSE_DATABASE_URL") or "").strip()
    if not raw:
        raw = (config.get_main_option("sqlalchemy.url") or "").strip()
    if not raw:
        raise RuntimeError(
            "No course database URL configured. Set COURSE_DATABASE_URL or sqlalchemy.url in alembic_course.ini."
        )

    if "your_course_db" in raw or "user:password@localhost" in raw:
        raise RuntimeError(
            "alembic_course.ini still contains the sample URL. "
            "Set COURSE_DATABASE_URL to your real course database connection string."
        )
    return _normalise_database_url(raw)


def run_migrations_offline() -> None:
    url = _resolved_url()
    context.configure(
        url=url,
        target_metadata=target_metadata,
        version_table="alembic_version_course",
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    section = config.get_section(config.config_ini_section, {})
    section["sqlalchemy.url"] = _resolved_url()
    connectable = engine_from_config(
        section,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            version_table="alembic_version_course",
            compare_type=True,
            render_as_batch=connection.dialect.name == "sqlite",
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
