"""One engine per lecturer's own database, created lazily and reused.

Mirrors app/db.py's engine construction exactly, just parameterized by a
decrypted connection string instead of the platform's own settings.database_url.
There is no eviction here yet - each engine this process has ever needed stays
open for the life of the process. Fine at the scale this is built for; worth
revisiting with an LRU or idle-timeout if the lecturer count grows large
enough for connection-pool pressure across many databases to matter.
"""

from collections.abc import Iterator

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from app.config import normalise_database_url
from app.models_course import CourseBase
from app.models_platform import Lecturer
from app.tenant_crypto import decrypt

_engines: dict[int, Engine] = {}
_sessionmakers: dict[int, sessionmaker] = {}


def _is_postgres(url: str) -> bool:
    return url.startswith("postgresql")


def _engine_kwargs(url: str) -> dict:
    if _is_postgres(url):
        return {
            "pool_pre_ping": True,
            "pool_size": 3,
            "max_overflow": 5,
            "pool_recycle": 900,
        }
    return {"connect_args": {"check_same_thread": False}}


def engine_for_url(raw_url: str) -> Engine:
    """A throwaway engine for a connection string that isn't (yet) a saved
    lecturer's - used only to validate a database before it's stored."""
    url = normalise_database_url(raw_url)
    return create_engine(url, future=True, **_engine_kwargs(url))


def _sessionmaker_for(lecturer: Lecturer) -> sessionmaker:
    cached = _sessionmakers.get(lecturer.id)
    if cached is not None:
        return cached
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
        raise
    finally:
        session.close()


def forget(lecturer_id: int) -> None:
    """Drop a cached engine - used when a lecturer reconnects a different
    database, so the next request doesn't keep talking to the old one."""
    engine = _engines.pop(lecturer_id, None)
    _sessionmakers.pop(lecturer_id, None)
    if engine is not None:
        engine.dispose()


def validate_schema(engine: Engine) -> list[str]:
    """Returns the list of expected course tables missing from this database.
    Empty means the connection matches docs/DATABASE_SCHEMA.md well enough to
    use - this never creates or alters anything, only inspects.
    """
    from sqlalchemy import inspect

    inspector = inspect(engine)
    existing = set(inspector.get_table_names())
    expected = set(CourseBase.metadata.tables.keys())
    return sorted(expected - existing)
