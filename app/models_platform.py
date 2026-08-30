"""Platform schema: lecturer accounts, the courses they run, and platform-level events.

This is the ONE database the app itself is deployed against (`DATABASE_URL`).

`Lecturer` is purely an account - who signs in, with what email and password. It
knows nothing about any course's students, exams or attempts. `Course` is what a
`Lecturer` actually runs: one row per course address (`slug`), holding branding,
where its data lives, and its own optional email settings. One lecturer can own many
courses; switching between them is just visiting a different course's URL with the
same account-level session - see app/deps.py's ownership check in current_admin.

Everything course-specific still lives in `app/models_course.py`, in whatever
database a `Course` configures - that part is unchanged by the account/course split.
"""

from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, JSON, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

MAIL_BACKENDS = ("", "console", "smtp", "resend")  # "" = fall back to the platform's own

from app.config import settings
from app.db import Base


class Lecturer(Base):
    """A signed-up account. Replaces the old env-var-provisioned Admin.

    Owns zero or more Course rows. Email verification lives here, once per person,
    not once per course - you prove you own an inbox once, then it's the account
    doing the proving.
    """

    __tablename__ = "lecturers"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    email: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(255))

    is_verified: Mapped[bool] = mapped_column(Boolean, default=False)
    verified_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    verification_sent_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    verification_token: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    password_set_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    courses: Mapped[list["Course"]] = relationship(back_populates="lecturer")


class Course(Base):
    """One course address (`slug`), owned by exactly one Lecturer account.

    `database_ready` only ever flips true after its database has been connected AND
    validated against the documented course schema - see docs/DATABASE_SCHEMA.md -
    never before.
    """

    __tablename__ = "courses"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    lecturer_id: Mapped[int] = mapped_column(
        ForeignKey("lecturers.id", ondelete="CASCADE"), index=True
    )
    slug: Mapped[str] = mapped_column(String(60), unique=True, index=True)

    course_code: Mapped[str] = mapped_column(String(50), default="")
    course_title: Mapped[str] = mapped_column(String(200), default="")
    institution: Mapped[str] = mapped_column(String(200), default="")

    # course_storage_mode chooses where this course's own data lives:
    # external = lecturer-managed database connection string (default)
    # platform = schema created in this deployment's platform DATABASE_URL
    course_storage_mode: Mapped[str] = mapped_column(String(20), default="external")
    # Encrypted with CREDENTIAL_ENCRYPTION_KEY (app/tenant_crypto.py), never stored
    # or logged in the clear. Nullable until the course completes setup.
    database_url_encrypted: Mapped[str | None] = mapped_column(Text, nullable=True)
    platform_db_schema: Mapped[str | None] = mapped_column(String(100), nullable=True)
    database_ready: Mapped[bool] = mapped_column(Boolean, default=False)

    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

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

    lecturer: Mapped[Lecturer] = relationship(back_populates="courses")

    # --- course-identity fields, mirroring the old env-var-driven Settings properties ---
    # so every place that used to read settings.brand/subtitle/footer/file_prefix can
    # read course.brand/subtitle/footer/file_prefix instead, unset course fields fall
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

    @property
    def email(self) -> str:
        """Where course alert/submission mail addressed to "the lecturer" goes -
        the owning account's own address. Kept as a property so every existing
        `course.email` call site (mirroring the old `lecturer.email`) keeps working
        unchanged."""
        return self.lecturer.email


class PlatformLog(Base):
    """Signup, login and database-connection events at the platform level.

    The course-scoped equivalent (AppLog, in app/models_course.py) lives in
    each course's own database; this table is for events that happen before
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
