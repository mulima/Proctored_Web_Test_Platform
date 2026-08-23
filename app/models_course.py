"""Course schema: everything one lecturer's course actually contains.

This is the schema documented in docs/DATABASE_SCHEMA.md as what a lecturer's
own database must already have before it can be connected. It is identical to
the app's original single-tenant schema, minus the old `admins` table - that
concept moved to `Lecturer` in app/models_platform.py, in the platform
database, not here.

`CourseBase` is intentionally its own declarative base, separate from the
platform's `Base` (app/db.py) - these tables are never created against this
app's own engine. A `Session` bound to whichever lecturer's database is
active for the current request (app/tenant_db.py) is what actually reads and
writes these models; the classes below are just the shared mapping, reusable
against any such session.

Two things here are still deliberately durable rather than convenient:

  * `app_logs` keeps every event a course emits, so a dispute months later can
    be reconstructed without the original browser or machine;
  * `snapshots` stores the evidence image bytes in the database rather than on
    disk, since a lecturer's own database is the only storage this app can
    assume survives a redeploy of the app itself.
"""

from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    JSON,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class CourseBase(DeclarativeBase):
    pass


class Student(CourseBase):
    __tablename__ = "students"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    full_name: Mapped[str] = mapped_column(String(200))
    email: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    computer_number: Mapped[str] = mapped_column(String(50), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(255))

    is_verified: Mapped[bool] = mapped_column(Boolean, default=False)
    verified_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    verification_sent_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # Single-use: cleared the moment the link is followed, so a forwarded email is inert.
    verification_token: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)

    is_approved: Mapped[bool] = mapped_column(Boolean, default=True)
    is_blocked: Mapped[bool] = mapped_column(Boolean, default=False)

    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    attempts: Mapped[list["Attempt"]] = relationship(back_populates="student")

    @property
    def can_sit(self) -> bool:
        return self.is_verified and self.is_approved and not self.is_blocked


class Exam(CourseBase):
    __tablename__ = "exams"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    code: Mapped[str] = mapped_column(String(50), default="")
    title: Mapped[str] = mapped_column(String(200))
    instructions: Mapped[str] = mapped_column(Text, default="")
    duration_minutes: Mapped[int] = mapped_column(Integer, default=90)
    total_marks: Mapped[int] = mapped_column(Integer, default=100)
    section_c_required: Mapped[int] = mapped_column(Integer, default=2)
    # Nothing can be sat until an admin opens it. This is the release switch.
    is_open: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    questions: Mapped[list["Question"]] = relationship(
        back_populates="exam", cascade="all, delete-orphan", order_by="Question.order_index"
    )
    attempts: Mapped[list["Attempt"]] = relationship(back_populates="exam")


class Question(CourseBase):
    __tablename__ = "questions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    exam_id: Mapped[int] = mapped_column(ForeignKey("exams.id", ondelete="CASCADE"), index=True)
    section: Mapped[str] = mapped_column(String(1))  # A multiple choice, B short, C long
    order_index: Mapped[int] = mapped_column(Integer, default=0)
    title: Mapped[str] = mapped_column(String(300), default="")
    prompt: Mapped[str] = mapped_column(Text)
    # Section A only: ["option one", "option two", ...]. Answers are stored as letters.
    options: Mapped[list | None] = mapped_column(JSON, nullable=True)
    marks: Mapped[int] = mapped_column(Integer, default=0)

    exam: Mapped[Exam] = relationship(back_populates="questions")

    __table_args__ = (Index("ix_questions_exam_section_order", "exam_id", "section", "order_index"),)


class Attempt(CourseBase):
    __tablename__ = "attempts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    exam_id: Mapped[int] = mapped_column(ForeignKey("exams.id"), index=True)
    student_id: Mapped[int] = mapped_column(ForeignKey("students.id"), index=True)

    started_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    deadline_at: Mapped[datetime] = mapped_column(DateTime)
    # The clock ratchet, carried over from the desktop build: elapsed time is the highest
    # reading ever recorded, so a tampered client clock can never hand time back.
    elapsed_high_water_seconds: Mapped[float] = mapped_column(Float, default=0.0)
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    current_question: Mapped[int] = mapped_column(Integer, default=0)
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    is_locked: Mapped[bool] = mapped_column(Boolean, default=False)
    submission_mode: Mapped[str] = mapped_column(String(20), default="")
    auto_submit_reason: Mapped[str] = mapped_column(Text, default="")

    strike_count: Mapped[int] = mapped_column(Integer, default=0)
    flagged: Mapped[bool] = mapped_column(Boolean, default=False)
    last_alert_email_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    pdf_filename: Mapped[str] = mapped_column(String(300), default="")
    pdf_bytes: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)

    exam: Mapped[Exam] = relationship(back_populates="attempts")
    student: Mapped[Student] = relationship(back_populates="attempts")
    answers: Mapped[list["Answer"]] = relationship(
        back_populates="attempt", cascade="all, delete-orphan"
    )
    incidents: Mapped[list["Incident"]] = relationship(
        back_populates="attempt", cascade="all, delete-orphan", order_by="Incident.occurred_at"
    )
    snapshots: Mapped[list["Snapshot"]] = relationship(
        back_populates="attempt", cascade="all, delete-orphan", order_by="Snapshot.captured_at"
    )

    # One sitting per student per exam. The database, not the client, enforces this.
    __table_args__ = (UniqueConstraint("exam_id", "student_id", name="uq_attempt_exam_student"),)


class Answer(CourseBase):
    __tablename__ = "answers"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    attempt_id: Mapped[int] = mapped_column(
        ForeignKey("attempts.id", ondelete="CASCADE"), index=True
    )
    question_id: Mapped[int] = mapped_column(ForeignKey("questions.id"), index=True)
    value: Mapped[str] = mapped_column(Text, default="")
    # Section C only: the candidate ticked this question for marking.
    selected: Mapped[bool] = mapped_column(Boolean, default=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )

    attempt: Mapped[Attempt] = relationship(back_populates="answers")
    question: Mapped[Question] = relationship()

    __table_args__ = (
        UniqueConstraint("attempt_id", "question_id", name="uq_answer_attempt_question"),
    )


class Incident(CourseBase):
    __tablename__ = "incidents"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    attempt_id: Mapped[int] = mapped_column(
        ForeignKey("attempts.id", ondelete="CASCADE"), index=True
    )
    category: Mapped[str] = mapped_column(String(40), index=True)
    label: Mapped[str] = mapped_column(String(120), default="")
    detail: Mapped[str] = mapped_column(Text, default="")
    source: Mapped[str] = mapped_column(String(20), default="browser")
    counted: Mapped[bool] = mapped_column(Boolean, default=False)
    occurred_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    elapsed_seconds: Mapped[int] = mapped_column(Integer, default=0)

    attempt: Mapped[Attempt] = relationship(back_populates="incidents")
    snapshots: Mapped[list["Snapshot"]] = relationship(back_populates="incident")


class Snapshot(CourseBase):
    __tablename__ = "snapshots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    attempt_id: Mapped[int] = mapped_column(
        ForeignKey("attempts.id", ondelete="CASCADE"), index=True
    )
    incident_id: Mapped[int | None] = mapped_column(
        ForeignKey("incidents.id", ondelete="SET NULL"), nullable=True, index=True
    )
    image: Mapped[bytes] = mapped_column(LargeBinary)
    mime: Mapped[str] = mapped_column(String(40), default="image/jpeg")
    byte_size: Mapped[int] = mapped_column(Integer, default=0)
    captured_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    # Second opinion: the same detector run server-side on this one frame. The browser
    # decides fast and cheaply; this is the slower, checkable answer.
    server_verdict: Mapped[str] = mapped_column(String(30), default="not_checked")
    server_confidence: Mapped[float] = mapped_column(Float, default=0.0)

    attempt: Mapped[Attempt] = relationship(back_populates="snapshots")
    incident: Mapped[Incident] = relationship(back_populates="snapshots")


class AppLog(CourseBase):
    __tablename__ = "app_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), index=True)
    level: Mapped[str] = mapped_column(String(10), default="INFO", index=True)
    event_type: Mapped[str] = mapped_column(String(60), index=True)
    message: Mapped[str] = mapped_column(Text, default="")
    student_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    attempt_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    ip: Mapped[str] = mapped_column(String(64), default="")
    user_agent: Mapped[str] = mapped_column(String(400), default="")
    payload: Mapped[dict | None] = mapped_column(JSON, nullable=True)
