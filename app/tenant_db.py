"""One engine per lecturer's own database, created lazily and reused.

Mirrors app/db.py's engine construction exactly, just parameterized by a
decrypted connection string instead of the platform's own settings.database_url.
There is no eviction here yet - each engine this process has ever needed stays
open for the life of the process. Fine at the scale this is built for; worth
revisiting with an LRU or idle-timeout if the lecturer count grows large
enough for connection-pool pressure across many databases to matter.
"""

from collections.abc import Iterator
import re

from sqlalchemy import create_engine, event, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from app.config import normalise_database_url, settings
from app.models_course import CourseBase
from app.models_platform import Lecturer
from app.monitoring import notify_operator
from app.tenant_crypto import decrypt

_engines: dict[int, Engine] = {}
_sessionmakers: dict[int, sessionmaker] = {}


def _is_postgres(url: str) -> bool:
    return url.startswith("postgresql")


def _engine_kwargs(url: str, schema: str | None = None) -> dict:
    if _is_postgres(url):
        kwargs = {
            "pool_pre_ping": True,
            "pool_size": 3,
            "max_overflow": 5,
            "pool_recycle": 900,
        }
        if schema:
            kwargs["connect_args"] = {"options": f"-csearch_path={schema}"}
        return kwargs
    return {"connect_args": {"check_same_thread": False}}


def engine_for_url(raw_url: str) -> Engine:
    """A throwaway engine for a connection string that isn't (yet) a saved
    lecturer's - used only to validate a database before it's stored."""
    url = normalise_database_url(raw_url)
    return create_engine(url, future=True, **_engine_kwargs(url))


def default_platform_schema_name(lecturer: Lecturer) -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", (lecturer.slug or "course").lower()).strip("_")
    slug = slug[:40] or "course"
    return f"course_{lecturer.id}_{slug}"


def provision_platform_schema(schema: str) -> None:
    """Creates a per-course schema inside the platform database and ensures all
    course tables exist there.
    """
    if not settings.is_postgres:
        raise RuntimeError("Platform-managed course storage requires PostgreSQL.")
    if not re.match(r"^[a-z_][a-z0-9_]{0,62}$", schema or ""):
        raise RuntimeError("Invalid schema name for platform-managed storage.")

    base_url = settings.sqlalchemy_url
    admin_engine = create_engine(base_url, future=True, **_engine_kwargs(base_url))
    try:
        with admin_engine.begin() as conn:
            conn.execute(text(f'CREATE SCHEMA IF NOT EXISTS "{schema}"'))
    finally:
        admin_engine.dispose()

    schema_engine = create_engine(
        base_url, future=True, **_engine_kwargs(base_url, schema=schema)
    )
    try:
        CourseBase.metadata.create_all(bind=schema_engine)
    finally:
        schema_engine.dispose()


def _sessionmaker_for(lecturer: Lecturer) -> sessionmaker:
    cached = _sessionmakers.get(lecturer.id)
    if cached is not None:
        return cached
    if lecturer.course_storage_mode == "platform":
        if not lecturer.platform_db_schema:
            raise RuntimeError(
                f"Lecturer {lecturer.slug!r} is set to platform storage but has no schema."
            )
        url = settings.sqlalchemy_url
        engine = create_engine(
            url,
            future=True,
            **_engine_kwargs(url, schema=lecturer.platform_db_schema),
        )
        # SECURITY FIX: Explicitly set search_path on every connection to prevent schema isolation failures
        # This ensures PostgreSQL connection pool doesn't reuse connections with wrong schema context
        schema_name = lecturer.platform_db_schema
        @event.listens_for(engine, "connect")
        def set_search_path(dbapi_conn, connection_record):
            cursor = dbapi_conn.cursor()
            cursor.execute(f"SET search_path TO \"{schema_name}\",public")
            cursor.close()
    else:
        if not lecturer.database_url_encrypted:
            raise RuntimeError(
                f"Lecturer {lecturer.slug!r} has no database configured yet."
            )
        raw_url = decrypt(lecturer.database_url_encrypted)
        url = normalise_database_url(raw_url)
        engine = create_engine(url, future=True, **_engine_kwargs(url))
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False, future=True)
    _engines[lecturer.id] = engine
    _sessionmakers[lecturer.id] = factory
    return factory


def course_session(lecturer: Lecturer) -> Iterator[Session]:
    """Same commit/rollback/close shape as app.db.get_db, just bound to
    whichever lecturer's own database this request belongs to."""
    factory = _sessionmaker_for(lecturer)
    session = factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        notify_operator("ClearGrade alert: COURSE_DATABASE_FAILURE", f"A course database request failed for lecturer {lecturer.id}. Check application logs for the exception.")
        raise
    finally:
        session.close()


def probe_course_connection(lecturer: Lecturer) -> str | None:
    """Returns None when the lecturer's configured course database is reachable,
    otherwise a short human-readable error string.
    """
    try:
        factory = _sessionmaker_for(lecturer)
        session = factory()
        try:
            session.execute(text("SELECT 1"))
        finally:
            session.close()
        return None
    except Exception as exc:
        # Drop potentially stale engine/sessionmaker so a repaired configuration
        # takes effect immediately on the next request.
        forget(lecturer.id)
        return f"{type(exc).__name__}: {str(exc)[:300]}"


def forget(lecturer_id: int) -> None:
    """Drop a cached engine - used when a lecturer reconnects a different
    database, so the next request doesn't keep talking to the old one."""
    engine = _engines.pop(lecturer_id, None)
    _sessionmakers.pop(lecturer_id, None)
    if engine is not None:
        engine.dispose()


def validate_schema(engine: Engine, schema: str | None = None) -> list[str]:
    """Returns the list of expected course tables missing from this database.
    Empty means the connection matches docs/DATABASE_SCHEMA.md well enough to
    use - this never creates or alters anything, only inspects.
    """
    from sqlalchemy import inspect

    inspector = inspect(engine)
    existing = set(inspector.get_table_names(schema=schema))
    expected = set(CourseBase.metadata.tables.keys())
    return sorted(expected - existing)
