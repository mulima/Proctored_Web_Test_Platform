"""Engine and session wiring for the PLATFORM database - lecturer accounts and their
course settings, not any course's own data. See app/models_platform.py for what
lives here, and app/tenant_db.py for the equivalent machinery built per-lecturer
against whatever database each one configures.

The schema is owned by Alembic, never by create_all. A deploy runs migrations in the
release phase; the app itself never alters tables, so data survives every redeploy.
"""

from collections.abc import Iterator

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.config import settings
from app.monitoring import notify_operator


class Base(DeclarativeBase):
    pass


def _engine_kwargs() -> dict:
    if settings.is_postgres:
        return {
            "pool_pre_ping": True,  # Railway drops idle connections
            # Deliberately well below Supabase's session-mode pooler cap (15 clients on
            # the free tier), and lower than the original post-incident value (4+6=10) -
            # since 2026-09-14, a platform-managed course's tenant engine (tenant_db.py)
            # also shares this same project/pooler, on top of the Supabase dashboard,
            # a deploy's own migration connection, and any external script. pool_size(3)
            # + max_overflow(3) = 6 leaves real headroom for those instead of this one
            # engine alone claiming most of the 15.
            "pool_size": 3,
            "max_overflow": 3,
            "pool_recycle": 900,
        }
    # SQLite, local development only.
    return {"connect_args": {"check_same_thread": False}}


engine = create_engine(settings.sqlalchemy_url, future=True, **_engine_kwargs())
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False, future=True)


def get_db() -> Iterator[Session]:
    session = SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        notify_operator("ClearGrade alert: PLATFORM_DATABASE_FAILURE", "The platform database request failed. Check application logs for the exception.")
        raise
    finally:
        session.close()
