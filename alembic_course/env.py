"""Alembic environment for the COURSE schema.

Deliberately independent of app/config.py - this is not the platform's own
database. The connection string comes from alembic_course.ini's own
sqlalchemy.url, exactly like a plain `alembic init` scaffold, since whoever
runs this (a lecturer provisioning their own database, or this repo
generating docs/DATABASE_SCHEMA.md via --sql offline mode) is never this
app's own deployment.
"""

from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

from app.models_course import CourseBase

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = CourseBase.metadata


def run_migrations_offline() -> None:
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
            render_as_batch=connection.dialect.name == "sqlite",
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
