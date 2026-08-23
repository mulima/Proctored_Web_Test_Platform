"""Platform schema: lecturer accounts and platform-level events.

This is the ONE database the app itself is deployed against (`DATABASE_URL`).
It knows nothing about any course's students, exams or attempts - only who has
signed up to run a course, what that course is called, and how to reach the
database they've pointed at their own data. Everything course-specific lives
in `app/models_course.py`, in whatever database a `Lecturer` configures.
"""

from datetime import datetime

from sqlalchemy import Boolean, DateTime, Integer, JSON, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

MAIL_BACKENDS = ("", "console", "smtp", "resend")  # "" = fall back to the platform's own

from app.config import settings
from app.db import Base


class Lecturer(Base):
    """A signed-up course owner. Replaces the old env-var-provisioned Admin.

    One lecturer runs exactly one course at one `slug`, in a database they
    control. `database_ready` only ever flips true after that database has
    been connected AND validated against the documented course schema - see
    docs/DATABASE_SCHEMA.md - never before.
    """

    __tablename__ = "lecturers"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    email: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    slug: Mapped[str] = mapped_column(String(60), unique=True, index=True)

    # Mirrors Student's verification fields exactly (app/models_course.py) - a
    # lecturer can't sign in until they've proved they own the email they signed up
    # with, same gate a student faces.
    is_verified: Mapped[bool] = mapped_column(Boolean, default=False)
    verified_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    verification_sent_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    verification_token: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)

    course_code: Mapped[str] = mapped_column(String(50), default="")
    course_title: Mapped[str] = mapped_column(String(200), default="")
    institution: Mapped[str] = mapped_column(String(200), default="")

    # Encrypted with CREDENTIAL_ENCRYPTION_KEY (app/tenant_crypto.py), never stored
    # or logged in the clear. Nullable until the lecturer completes setup.
    database_url_encrypted: Mapped[str | None] = mapped_column(Text, nullable=True)
    database_ready: Mapped[bool] = mapped_column(Boolean, default=False)

    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    password_set_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    # --- this course's own email delivery, optional - see app/mailer.py's resolve() ---
    # Every field left blank/unset falls back to the platform's own MAIL_BACKEND etc.
    # (app/config.py), the same "runs with nothing configured" default the platform
    # itself has always had. mail_backend="" specifically means "use the platform's",
    # not "use console" - a lecturer who wants console explicitly can set it.
    mail_backend: Mapped[str] = mapped_column(String(10), default="")
    mail_from: Mapped[str] = mapped_column(String(255), default="")
    smtp_host: Mapped[str] = mapped_column(String(255), default="")
    smtp_port: Mapped[int | None] = mapped_column(Integer, nullable=True)
    smtp_username: Mapped[str] = mapped_column(String(255), default="")
    smtp_password_encrypted: Mapped[str | None] = mapped_column(Text, nullable=True)
    smtp_use_tls: Mapped[bool] = mapped_column(Boolean, default=True)
    resend_api_key_encrypted: Mapped[str | None] = mapped_column(Text, nullable=True)

    # --- course-identity fields, mirroring the old env-var-driven Settings properties ---
    # so every place that used to read settings.brand/subtitle/footer/file_prefix can
    # read lecturer.brand/subtitle/footer/file_prefix instead, unset course fields fall
    # back to something generic rather than a blank header or a filename starting "_".

    @property
    def brand(self) -> str:
        return self.course_code or settings.app_name

    @property
    def subtitle(self) -> str:
        return self.course_title or "Proctored online assessment"

    @property
    def file_prefix(self) -> str:
        return self.course_code or "exam"

    @property
    def footer(self) -> str:
        return self.institution or settings.app_name


class PlatformLog(Base):
    """Signup, login and database-connection events at the platform level.

    The course-scoped equivalent (AppLog, in app/models_course.py) lives in
    each lecturer's own database; this table is for events that happen before
    or outside any course database is even reachable.
    """

    __tablename__ = "platform_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), index=True)
    level: Mapped[str] = mapped_column(String(10), default="INFO", index=True)
    event_type: Mapped[str] = mapped_column(String(60), index=True)
    message: Mapped[str] = mapped_column(Text, default="")
    lecturer_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    ip: Mapped[str] = mapped_column(String(64), default="")
    user_agent: Mapped[str] = mapped_column(String(400), default="")
    payload: Mapped[dict | None] = mapped_column(JSON, nullable=True)
