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

from sqlalchemy import create_engine, event, select, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from app.config import normalise_database_url, settings
from app.models_course import CourseBase, Student
from app.models_platform import Course, Lecturer
from app.monitoring import notify_operator
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


def _attach_search_path(engine: Engine, schema: str) -> None:
    """Forces every new DBAPI connection on this engine to search ONLY the given
    schema - deliberately no ",public" fallback. A course provisioned in
    platform-managed storage mode must never silently read or write another
    course's tables just because its own schema is missing one; a loud failure
    there is far better than quiet cross-course data exposure.

    This replaces an earlier approach that passed `-csearch_path=<schema>` as a
    libpq connect option - missing the required space before `search_path`, which
    libpq silently ignores rather than erroring on. Every connection that was
    supposed to be schema-scoped was actually just using the database's default
    search_path (public) the entire time: provision_platform_schema()'s create_all
    found tables of the same name already sitting in public (from a course
    connected in "external" mode to this same database) and created nothing in the
    new schema, and every later query for that course fell straight through to the
    shared public tables too. This SET runs as a real, explicit statement on every
    new connection, so it doesn't depend on a connection-string being parsed
    correctly at all.
    """

    @event.listens_for(engine, "connect")
    def set_search_path(dbapi_conn, connection_record):
        cursor = dbapi_conn.cursor()
        cursor.execute(f'SET search_path TO "{schema}"')
        cursor.close()


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
    """Creates a per-course schema inside the platform database, creates every
    course table there, and verifies they actually exist in that schema before
    returning. Never trust create_all() alone to mean "it worked" - a
    misconfigured search_path can make it silently create nothing at all (see
    _attach_search_path's docstring for exactly how that happened here once).
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

    schema_engine = create_engine(base_url, future=True, **_engine_kwargs(base_url))
    _attach_search_path(schema_engine, schema)
    try:
        CourseBase.metadata.create_all(bind=schema_engine)
        missing = validate_schema(schema_engine, schema=schema)
        if missing:
            raise RuntimeError(
                f"Provisioning did not create the expected table(s) in {schema!r}: "
                + ", ".join(missing) + ". Nothing was activated."
            )
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
        engine = create_engine(url, future=True, **_engine_kwargs(url))
        _attach_search_path(engine, lecturer.platform_db_schema)
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


def fetch_course_students(course: Course, timeout: int = 5) -> list:
    """Read-only, timeout-bounded student list for one course.

    Used by the cross-course "all my students" dashboard, which opens several
    courses' databases in a single page load - one slow or unreachable course must
    not stall the others. Deliberately builds its own short-lived engine rather than
    reusing the shared cached one from _sessionmaker_for(): a bounded connect
    timeout only makes sense for this read-only summary view, not for that course's
    normal, already-working request path elsewhere in the app, so it stays local to
    this function instead of changing behaviour everywhere.
    """
    if course.course_storage_mode == "platform":
        if not course.platform_db_schema:
            raise RuntimeError("Course has no platform schema configured yet.")
        url = settings.sqlalchemy_url
    else:
        if not course.database_url_encrypted:
            raise RuntimeError("Course has no database configured yet.")
        url = normalise_database_url(decrypt(course.database_url_encrypted))
    kwargs = _engine_kwargs(url)

    if _is_postgres(url):
        kwargs = dict(kwargs)
        connect_args = dict(kwargs.get("connect_args") or {})
        connect_args["connect_timeout"] = timeout
        kwargs["connect_args"] = connect_args

    engine = create_engine(url, future=True, **kwargs)
    if course.course_storage_mode == "platform":
        _attach_search_path(engine, course.platform_db_schema)
    try:
        with Session(engine) as session:
            rows = session.execute(
                select(
                    Student.id,
                    Student.full_name,
                    Student.email,
                    Student.computer_number,
                    Student.is_verified,
                    Student.is_approved,
                    Student.is_blocked,
                    Student.created_at,
                ).order_by(Student.computer_number)
            ).all()
            return list(rows)
    finally:
        engine.dispose()


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
