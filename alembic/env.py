"""Alembic environment for the PLATFORM schema (lecturer accounts and their course
settings) - the one database this app itself is deployed against, via DATABASE_URL.

This has nothing to do with any lecturer's own course database. That schema is
tracked separately, in alembic_course/, and is never migrated by this app - see
docs/DATABASE_SCHEMA.md.
"""

from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

from app.config import settings
from app.db import Base
from app import models_platform  # noqa: F401  - imported for its side effect of registering tables

config = context.config
config.set_main_option("sqlalchemy.url", settings.sqlalchemy_url)
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    context.configure(
        url=settings.sqlalchemy_url,
        target_metadata=target_metadata,
        version_table="alembic_version_platform",
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
            version_table="alembic_version_platform",
            compare_type=True,
            render_as_batch=connection.dialect.name == "sqlite",
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
