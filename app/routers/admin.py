"""Admin panel: exam and question setup, roster, results, evidence, logs - and,
new here, the course's own setup: database connection and branding.

A lecturer's identity and password live in the platform database as an *account*
(app/models_platform.py's Lecturer), separate from any one course - one account can
own several Course rows. Every route below except /login and /setup itself
additionally requires the course database to be connected and validated
(require_admin_ready) - see app/deps.py. Course-switching (moving between courses
the signed-in account owns) needs no special handling here: the session cookie isn't
tied to a course, so simply visiting a different owned course's /{slug}/admin/...
passes the ownership check in require_admin without a fresh login.
"""

import json
import hashlib
import csv
from datetime import datetime
from io import BytesIO, StringIO
from zipfile import ZIP_DEFLATED, ZipFile

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from sqlalchemy import desc, func, select
from sqlalchemy.orm import Session, joinedload, selectinload

from app import logging_service, pdf, proctor, vision
from app.config import settings
from app.db import get_db
from app.deps import get_course, get_course_db, require_admin, require_admin_ready, templates
from app.monitoring import repeated_platform_event
from app.models_course import (
    AppLog,
    Attempt,
    Exam,
    Question,
    Snapshot,
    Student,
    SubmissionAuditEvent,
)
from app.models_platform import Course, Lecturer
from app.security import set_session_cookie, verify_password
from app.tenant_crypto import CredentialEncryptionError, encrypt
from app.tenant_db import (
    default_platform_schema_name,
    engine_for_url,
    forget,
    probe_course_connection,
    provision_platform_schema,
    validate_schema,
)

router = APIRouter(prefix="/admin")


def _sha256(value: bytes | None) -> str:
    if not value:
        return ""
    return hashlib.sha256(value).hexdigest()


def _record_submission_audit(
    db: Session,
    *,
    attempt: Attempt,
    action: str,
    status: str,
    actor: str,
    message: str,
    stored_sha256: str = "",
    current_sha256: str = "",
    is_match: bool | None = None,
    payload: dict | None = None,
) -> None:
    db.add(
        SubmissionAuditEvent(
            attempt_id=attempt.id,
            exam_id=attempt.exam_id,
            action=action,
            status=status,
            actor=actor,
            message=message,
            stored_pdf_sha256=stored_sha256,
            current_pdf_sha256=current_sha256,
            is_match=is_match,
            payload=payload,
        )
    )


def _exam_audit_rows(attempts: list[Attempt]) -> list[dict]:
    rows: list[dict] = []
    for attempt in attempts:
        events = sorted(list(attempt.audit_events), key=lambda item: item.id)
        regenerate_count = sum(1 for event in events if event.action == "regenerate")
        compare_count = sum(1 for event in events if event.action == "compare")
        mismatch_count = sum(
            1
            for event in events
            if event.action == "compare" and event.is_match is False
        )
        review_count = sum(1 for event in events if event.action == "review")
        last_action = events[-1] if events else None

        rows.append(
            {
                "attempt_id": attempt.id,
                "student_id": attempt.student.id,
                "student_computer_number": attempt.student.computer_number,
                "student_name": attempt.student.full_name,
                "student_email": attempt.student.email,
                "submitted_at": attempt.submitted_at.isoformat() if attempt.submitted_at else "",
                "is_locked": bool(attempt.is_locked),
                "submission_mode": attempt.submission_mode or "",
                "flagged": bool(attempt.flagged),
                "strike_count": int(attempt.strike_count or 0),
                "snapshot_count": len(attempt.snapshots),
                "reviewed_at": attempt.reviewed_at.isoformat() if attempt.reviewed_at else "",
                "reviewed_by": attempt.reviewed_by or "",
                "review_notes": attempt.review_notes or "",
                "last_pdf_audit_at": (
                    attempt.last_pdf_audit_at.isoformat() if attempt.last_pdf_audit_at else ""
                ),
                "last_pdf_audit_match": attempt.last_pdf_audit_match,
                "last_pdf_audit_stored_sha256": attempt.last_pdf_audit_stored_sha256 or "",
                "last_pdf_audit_current_sha256": attempt.last_pdf_audit_current_sha256 or "",
                "last_pdf_audit_message": attempt.last_pdf_audit_message or "",
                "regeneration_count": regenerate_count,
                "compare_count": compare_count,
                "compare_mismatch_count": mismatch_count,
                "review_mark_count": review_count,
                "history_event_count": len(events),
                "last_history_action": last_action.action if last_action else "",
                "last_history_status": last_action.status if last_action else "",
                "last_history_actor": last_action.actor if last_action else "",
                "last_history_at": (
                    last_action.at.isoformat() if last_action and last_action.at else ""
                ),
            }
        )
    return rows


@router.get("/login", response_class=HTMLResponse)
def login_form(request: Request, course: Course = Depends(get_course)):
    return templates.TemplateResponse(
        request, "admin/login.html", {"errors": [], "prefill_email": ""}
    )


@router.post("/login")
def login(
    request: Request,
    email: str = Form(...),
    password: str = Form(...),
    course: Course = Depends(get_course),
    platform_db: Session = Depends(get_db),
):
    email = email.strip().lower()
    account = platform_db.scalar(select(Lecturer).where(func.lower(Lecturer.email) == email))

    def _reject(message: str, status_code: int = 401):
        logging_service.record_platform(
            platform_db,
            "ADMIN_LOGIN_FAILED",
            email,
            level="WARNING",
            lecturer_id=account.id if account else None,
            request=request,
        )
        repeated_platform_event(
            platform_db,
            "ADMIN_LOGIN_FAILED",
            request=request,
            message="Repeated lecturer login failures detected.",
        )
        return templates.TemplateResponse(
            request,
            "admin/login.html",
            {"errors": [message], "prefill_email": email},
            status_code=status_code,
        )

    if account is None or not verify_password(password, account.password_hash):
        # Same message whether the email or the password was wrong, and whether or
        # not this account owns *this* course - a login form is not the place to
        # reveal who runs what.
        return _reject("Email or password is incorrect.")
    if course.lecturer_id != account.id:
        return _reject("Email or password is incorrect.")
    if not account.is_verified:
        return templates.TemplateResponse(
            request,
            "admin/login.html",
            {
                "errors": [
                    "Confirm your email address first. Check your inbox for the link, "
                    "or resend it at /resend-lecturer."
                ],
                "prefill_email": email,
            },
            status_code=403,
        )

    if course.database_ready:
        db_probe_error = probe_course_connection(course)
        if db_probe_error:
            logging_service.record_platform(
                platform_db,
                "ADMIN_LOGIN_DB_UNAVAILABLE",
                db_probe_error,
                level="ERROR",
                lecturer_id=account.id,
                request=request,
            )
            response = RedirectResponse(
                f"/{course.slug}/admin/setup?db_unavailable=1",
                status_code=303,
            )
            set_session_cookie(response, role="admin", id=account.id)
            return response

    logging_service.record_platform(
        platform_db, "ADMIN_LOGIN", email, lecturer_id=account.id, request=request
    )
    response = RedirectResponse(f"/{course.slug}/admin", status_code=303)
    set_session_cookie(response, role="admin", id=account.id)
    return response


# ------------------------------------------------------------------------------ setup


@router.get("/setup", response_class=HTMLResponse)
def setup_form(
    request: Request,
    admin: Lecturer = Depends(require_admin),
    course: Course = Depends(get_course),
):
    return templates.TemplateResponse(
        request, "admin/setup.html", _setup_context(course, [])
    )


def _setup_context(course: Course, errors: list[str]) -> dict:
    storage_mode = course.course_storage_mode or "external"
    return {
        "errors": errors,
        "lecturer": course,
        "storage_mode": storage_mode,
        "has_database": bool(course.database_url_encrypted) or bool(course.platform_db_schema),
        "has_smtp_password": bool(course.smtp_password_encrypted),
        "has_resend_key": bool(course.resend_api_key_encrypted),
    }


@router.post("/setup/database", response_class=HTMLResponse)
def setup_database(
    request: Request,
    storage_mode: str = Form("external"),
    database_url: str = Form(""),
    admin: Lecturer = Depends(require_admin),
    course: Course = Depends(get_course),
    platform_db: Session = Depends(get_db),
):
    errors: list[str] = []
    storage_mode = (storage_mode or "external").strip().lower()
    if storage_mode not in {"external", "platform"}:
        storage_mode = "external"

    if storage_mode == "platform":
        try:
            schema = course.platform_db_schema or default_platform_schema_name(course)
            provision_platform_schema(schema)
        except Exception as exc:
            errors.append(
                "Could not provision course tables in the platform database: "
                f"{type(exc).__name__}: {str(exc)[:300]}"
            )
        else:
            course.course_storage_mode = "platform"
            course.platform_db_schema = schema
            course.database_url_encrypted = None
            course.database_ready = True
            forget(course.id)
            logging_service.record_platform(
                platform_db,
                "LECTURER_DATABASE_CONNECTED_PLATFORM",
                f"{course.slug} -> schema {schema}",
                lecturer_id=admin.id,
                request=request,
            )
            platform_db.commit()
            destination = f"/{course.slug}/admin" if course.database_ready else f"/{course.slug}/admin/setup?saved=1"
            return RedirectResponse(destination, status_code=303)

    database_url = database_url.strip()
    if not database_url:
        errors.append("Paste a database connection string.")
    else:
        try:
            engine = engine_for_url(database_url)
            try:
                missing = validate_schema(engine)
            finally:
                engine.dispose()
        except Exception as exc:
            errors.append(f"Could not connect: {type(exc).__name__}: {str(exc)[:300]}")
        else:
            if missing:
                errors.append(
                    "Connected, but this database is missing expected table(s): "
                    + ", ".join(missing)
                    + ". See /docs/DATABASE_SCHEMA.md - this app never creates tables for "
                    "you, only checks for them."
                )
            else:
                try:
                    encrypted = encrypt(database_url)
                except CredentialEncryptionError as exc:
                    # A misconfigured deployment (CREDENTIAL_ENCRYPTION_KEY missing or
                    # invalid), not something the lecturer did wrong - say so plainly
                    # instead of a raw 500, and don't half-save.
                    errors.append(
                        "This deployment can't store a database connection right now: "
                        f"{exc}. Ask whoever runs this platform to check "
                        "CREDENTIAL_ENCRYPTION_KEY."
                    )
                else:
                    course.course_storage_mode = "external"
                    course.platform_db_schema = None
                    course.database_url_encrypted = encrypted
                    course.database_ready = True
                    forget(course.id)  # drop any previously cached engine for this course
                    logging_service.record_platform(
                        platform_db,
                        "LECTURER_DATABASE_CONNECTED",
                        course.slug,
                        lecturer_id=admin.id,
                        request=request,
                    )

    platform_db.commit()

    if errors:
        return templates.TemplateResponse(
            request, "admin/setup.html", _setup_context(course, errors), status_code=400
        )
    destination = f"/{course.slug}/admin" if course.database_ready else f"/{course.slug}/admin/setup?saved=1"
    return RedirectResponse(destination, status_code=303)


@router.post("/setup/branding", response_class=HTMLResponse)
def setup_branding(
    request: Request,
    course_code: str = Form(""),
    course_title: str = Form(""),
    institution: str = Form(""),
    admin: Lecturer = Depends(require_admin),
    course: Course = Depends(get_course),
    platform_db: Session = Depends(get_db),
):
    course.course_code = course_code.strip()
    course.course_title = course_title.strip()
    course.institution = institution.strip()
    platform_db.commit()
    return RedirectResponse(f"/{course.slug}/admin/setup?saved=1", status_code=303)


@router.post("/setup/email", response_class=HTMLResponse)
def setup_email(
    request: Request,
    mail_backend: str = Form(""),
    mail_from: str = Form(""),
    smtp_host: str = Form(""),
    smtp_port: str = Form(""),
    smtp_username: str = Form(""),
    smtp_password: str = Form(""),
    smtp_use_tls: str = Form(""),
    resend_api_key: str = Form(""),
    admin: Lecturer = Depends(require_admin),
    course: Course = Depends(get_course),
    platform_db: Session = Depends(get_db),
):
    """Every field here is optional - blank means "use the platform's own", the
    same fallback the platform itself has always had (MAIL_BACKEND=console with
    nothing else set). Secrets (smtp_password, resend_api_key) are never shown back
    once saved; leaving them blank on a later edit keeps whatever's already stored,
    exactly like the database connection string above."""
    errors: list[str] = []
    mail_backend = mail_backend.strip().lower()
    if mail_backend and mail_backend not in ("console", "smtp", "resend"):
        errors.append("Mail backend must be console, smtp, or resend.")
    if mail_backend == "smtp" and not (smtp_host.strip() or course.smtp_host):
        errors.append("SMTP host is required when the mail backend is smtp.")
    if mail_backend == "resend" and not (resend_api_key.strip() or course.resend_api_key_encrypted):
        errors.append("A Resend API key is required when the mail backend is resend.")

    if errors:
        return templates.TemplateResponse(
            request, "admin/setup.html", _setup_context(course, errors), status_code=400
        )

    course.mail_backend = mail_backend
    course.mail_from = mail_from.strip()

    if mail_backend == "smtp":
        course.smtp_host = smtp_host.strip()
        course.smtp_port = int(smtp_port) if smtp_port.strip().isdigit() else None
        course.smtp_username = smtp_username.strip()
        course.smtp_use_tls = bool(smtp_use_tls)
        if smtp_password.strip():
            course.smtp_password_encrypted = encrypt(smtp_password.strip())
    else:
        # Not using this course's own SMTP - clear it out rather than leaving stale
        # values sitting in the row. resolve() (app/mailer.py) already treats a blank
        # mail_backend as "use the platform's", but a non-empty smtp_host left behind
        # from an earlier choice is enough to make it think this course still wants
        # its own SMTP, silently shadowing the platform's real credentials. Emptying
        # these fields here is what actually makes "use the platform's default" mean
        # that, not just show that in the form.
        course.smtp_host = ""
        course.smtp_port = None
        course.smtp_username = ""
        course.smtp_password_encrypted = None
        course.smtp_use_tls = True

    if mail_backend == "resend":
        if resend_api_key.strip():
            course.resend_api_key_encrypted = encrypt(resend_api_key.strip())
    else:
        course.resend_api_key_encrypted = None

    platform_db.commit()
    logging_service.record_platform(
        platform_db, "LECTURER_EMAIL_UPDATED", course.slug, lecturer_id=admin.id, request=request
    )
    return RedirectResponse(f"/{course.slug}/admin/setup?saved=1", status_code=303)


@router.get("", response_class=HTMLResponse)
def home(
    request: Request,
    course: Course = Depends(require_admin_ready),
    db: Session = Depends(get_course_db),
):
    exams = db.scalars(select(Exam).order_by(Exam.id.desc())).all()
    student_count = db.scalar(select(func.count(Student.id))) or 0
    verified_count = (
        db.scalar(select(func.count(Student.id)).where(Student.is_verified.is_(True))) or 0
    )
    attempts = db.scalars(select(Attempt).order_by(desc(Attempt.id)).limit(25)).all()
    flagged_count = (
        db.scalar(select(func.count(Attempt.id)).where(Attempt.flagged.is_(True))) or 0
    )
    return templates.TemplateResponse(
        request,
        "admin/home.html",
        {
            "exams": exams,
            "student_count": student_count,
            "verified_count": verified_count,
            "attempts": attempts,
            "flagged_count": flagged_count,
            "vision_ready": vision.available(),
            "vision_reason": vision.unavailable_reason(),
        },
    )


# ------------------------------------------------------------------ exams and questions


@router.get("/json-guide", response_class=HTMLResponse)
def json_guide(
    request: Request,
    course: Course = Depends(require_admin_ready),
):
    return templates.TemplateResponse(request, "admin/json_guide.html", {})


@router.post("/exams")
def create_exam(
    request: Request,
    title: str = Form(...),
    duration_minutes: int = Form(90),
    total_marks: int = Form(100),
    section_c_required: int = Form(2),
    course: Course = Depends(require_admin_ready),
    db: Session = Depends(get_course_db),
):
    exam = Exam(
        code=course.course_code,
        title=title.strip(),
        duration_minutes=max(1, duration_minutes),
        total_marks=total_marks,
        section_c_required=max(0, section_c_required),
        is_open=False,
    )
    db.add(exam)
    db.commit()
    logging_service.record(db, "EXAM_CREATED", exam.title, request=request)
    return RedirectResponse(f"/{course.slug}/admin/exams/{exam.id}", status_code=303)


@router.get("/exams/{exam_id}", response_class=HTMLResponse)
def exam_detail(
    exam_id: int,
    request: Request,
    course: Course = Depends(require_admin_ready),
    db: Session = Depends(get_course_db),
):
    exam = db.get(Exam, exam_id)
    if exam is None:
        return RedirectResponse(f"/{course.slug}/admin", status_code=303)
    questions = sorted(exam.questions, key=lambda q: (q.section, q.order_index))
    attempt_count = (
        db.scalar(select(func.count(Attempt.id)).where(Attempt.exam_id == exam.id)) or 0
    )
    return templates.TemplateResponse(
        request,
        "admin/exam.html",
        {
            "exam": exam,
            "questions": questions,
            "sections": {
                "A": [q for q in questions if q.section == "A"],
                "B": [q for q in questions if q.section == "B"],
                "C": [q for q in questions if q.section == "C"],
            },
            "attempt_count": attempt_count,
        },
    )


@router.post("/exams/{exam_id}/regenerate-pdfs")
def regenerate_exam_pdfs(
    exam_id: int,
    request: Request,
    admin: Lecturer = Depends(require_admin),
    course: Course = Depends(require_admin_ready),
    db: Session = Depends(get_course_db),
):
    exam = db.get(Exam, exam_id)
    if exam is None:
        return RedirectResponse(f"/{course.slug}/admin", status_code=303)

    attempts = db.scalars(
        select(Attempt)
        .where(
            Attempt.exam_id == exam.id,
            Attempt.is_locked.is_(True),
            Attempt.submitted_at.is_not(None),
        )
        .options(
            joinedload(Attempt.student),
            joinedload(Attempt.exam).options(selectinload(Exam.questions)),
            selectinload(Attempt.answers),
            selectinload(Attempt.incidents),
        )
        .order_by(Attempt.id)
    ).all()

    try:
        for attempt in attempts:
            old_sha256 = _sha256(attempt.pdf_bytes)
            answers_by_question = {answer.question_id: answer for answer in attempt.answers}
            rebuilt = pdf.build(attempt, answers_by_question, course)
            rebuilt_sha256 = _sha256(rebuilt)
            attempt.pdf_bytes = rebuilt
            attempt.pdf_filename = pdf.filename_for(attempt, course)
            _record_submission_audit(
                db,
                attempt=attempt,
                action="regenerate",
                status="ok",
                actor=admin.email,
                message="Submission PDF regenerated from current records.",
                stored_sha256=old_sha256,
                current_sha256=rebuilt_sha256,
                is_match=(old_sha256 == rebuilt_sha256) if old_sha256 else None,
            )
    except Exception as exc:
        db.rollback()
        from app.monitoring import course_alert

        course_alert(
            db,
            "PDF_REGENERATION_FAILED",
            f"PDF regeneration failed for exam {exam.id}: {type(exc).__name__}",
            request=request,
            payload={"exam_id": exam.id},
        )
        return RedirectResponse(
            f"/{course.slug}/admin/exams/{exam.id}?error=regeneration",
            status_code=303,
        )

    db.commit()
    logging_service.record(
        db,
        "PDFS_REGENERATED",
        f"Regenerated {len(attempts)} PDF(s) for {exam.title}",
        level="WARNING",
        request=request,
        payload={"exam_id": exam.id, "attempt_count": len(attempts)},
    )
    return RedirectResponse(
        f"/{course.slug}/admin/exams/{exam.id}?regenerated={len(attempts)}",
        status_code=303,
    )


@router.get("/exams/{exam_id}/submission-pdfs.zip")
def exam_submission_pdfs_zip(
    exam_id: int,
    course: Course = Depends(require_admin_ready),
    db: Session = Depends(get_course_db),
):
    exam = db.get(Exam, exam_id)
    if exam is None:
        return JSONResponse({"error": "Exam not found."}, status_code=404)

    attempts = db.scalars(
        select(Attempt)
        .where(
            Attempt.exam_id == exam.id,
            Attempt.is_locked.is_(True),
            Attempt.submitted_at.is_not(None),
            Attempt.pdf_bytes.is_not(None),
        )
        .options(joinedload(Attempt.student), joinedload(Attempt.exam))
        .order_by(Attempt.id)
    ).all()

    archive = BytesIO()
    with ZipFile(archive, mode="w", compression=ZIP_DEFLATED) as bundle:
        for attempt in attempts:
            filename = (attempt.pdf_filename or "").strip() or pdf.filename_for(attempt, course)
            if not filename.lower().endswith(".pdf"):
                filename += ".pdf"
            # Prefix attempt id to keep names stable and unique across same-student retries.
            bundle.writestr(f"attempt_{attempt.id}_{filename}", attempt.pdf_bytes or b"")

    filename = f"submission_pdfs_exam_{exam.id}.zip"
    return Response(
        content=archive.getvalue(),
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/exams/{exam_id}/audit-report.json")
def exam_audit_report_json(
    exam_id: int,
    course: Course = Depends(require_admin_ready),
    db: Session = Depends(get_course_db),
):
    exam = db.get(Exam, exam_id)
    if exam is None:
        return JSONResponse({"error": "Exam not found."}, status_code=404)

    attempts = db.scalars(
        select(Attempt)
        .where(Attempt.exam_id == exam.id)
        .options(
            joinedload(Attempt.student),
            selectinload(Attempt.snapshots),
            selectinload(Attempt.audit_events),
        )
        .order_by(Attempt.id)
    ).all()
    rows = _exam_audit_rows(attempts)

    payload = {
        "exam": {
            "id": exam.id,
            "title": exam.title,
            "code": exam.code,
        },
        "generated_at": datetime.utcnow().isoformat(),
        "summary": {
            "attempt_count": len(rows),
            "submitted_count": sum(1 for row in rows if row["submitted_at"]),
            "flagged_count": sum(1 for row in rows if row["flagged"]),
            "reviewed_count": sum(1 for row in rows if row["reviewed_at"]),
            "mismatch_count": sum(1 for row in rows if row["last_pdf_audit_match"] is False),
        },
        "attempts": rows,
    }

    filename = f"audit_exam_{exam.id}.json"
    return Response(
        content=json.dumps(payload, indent=2, default=str),
        media_type="application/json",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/exams/{exam_id}/audit-report.csv")
def exam_audit_report_csv(
    exam_id: int,
    course: Course = Depends(require_admin_ready),
    db: Session = Depends(get_course_db),
):
    exam = db.get(Exam, exam_id)
    if exam is None:
        return JSONResponse({"error": "Exam not found."}, status_code=404)

    attempts = db.scalars(
        select(Attempt)
        .where(Attempt.exam_id == exam.id)
        .options(
            joinedload(Attempt.student),
            selectinload(Attempt.snapshots),
            selectinload(Attempt.audit_events),
        )
        .order_by(Attempt.id)
    ).all()
    rows = _exam_audit_rows(attempts)

    columns = [
        "attempt_id",
        "student_id",
        "student_computer_number",
        "student_name",
        "student_email",
        "submitted_at",
        "is_locked",
        "submission_mode",
        "flagged",
        "strike_count",
        "snapshot_count",
        "reviewed_at",
        "reviewed_by",
        "review_notes",
        "last_pdf_audit_at",
        "last_pdf_audit_match",
        "last_pdf_audit_stored_sha256",
        "last_pdf_audit_current_sha256",
        "last_pdf_audit_message",
        "regeneration_count",
        "compare_count",
        "compare_mismatch_count",
        "review_mark_count",
        "history_event_count",
        "last_history_action",
        "last_history_status",
        "last_history_actor",
        "last_history_at",
    ]
    buffer = StringIO()
    writer = csv.DictWriter(buffer, fieldnames=columns)
    writer.writeheader()
    for row in rows:
        writer.writerow({key: row.get(key, "") for key in columns})

    filename = f"audit_exam_{exam.id}.csv"
    return Response(
        content=buffer.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.post("/attempts/{attempt_id}/regenerate-pdf")
def regenerate_attempt_pdf(
    attempt_id: int,
    request: Request,
    admin: Lecturer = Depends(require_admin),
    course: Course = Depends(require_admin_ready),
    db: Session = Depends(get_course_db),
):
    attempt = db.scalar(
        select(Attempt)
        .where(Attempt.id == attempt_id)
        .options(
            joinedload(Attempt.student),
            joinedload(Attempt.exam).options(selectinload(Exam.questions)),
            selectinload(Attempt.answers),
            selectinload(Attempt.incidents),
        )
    )
    if attempt is None:
        return RedirectResponse(f"/{course.slug}/admin/attempts", status_code=303)

    old_sha256 = _sha256(attempt.pdf_bytes)
    answers_by_question = {answer.question_id: answer for answer in attempt.answers}
    rebuilt = pdf.build(attempt, answers_by_question, course)
    attempt.pdf_bytes = rebuilt
    attempt.pdf_filename = pdf.filename_for(attempt, course)
    _record_submission_audit(
        db,
        attempt=attempt,
        action="regenerate",
        status="ok",
        actor=admin.email,
        message="Submission PDF regenerated from current records.",
        stored_sha256=old_sha256,
        current_sha256=_sha256(rebuilt),
        is_match=(old_sha256 == _sha256(rebuilt)) if old_sha256 else None,
    )
    db.commit()
    logging_service.record(
        db,
        "PDF_REGENERATED",
        f"Attempt {attempt.id}",
        request=request,
        payload={"attempt_id": attempt.id, "exam_id": attempt.exam_id},
    )
    return RedirectResponse(
        f"/{course.slug}/admin/attempts/{attempt.id}?regen=1",
        status_code=303,
    )


@router.post("/attempts/{attempt_id}/compare-pdf")
def compare_attempt_pdf(
    attempt_id: int,
    request: Request,
    admin: Lecturer = Depends(require_admin),
    course: Course = Depends(require_admin_ready),
    db: Session = Depends(get_course_db),
):
    attempt = db.scalar(
        select(Attempt)
        .where(Attempt.id == attempt_id)
        .options(
            joinedload(Attempt.student),
            joinedload(Attempt.exam).options(selectinload(Exam.questions)),
            selectinload(Attempt.answers),
            selectinload(Attempt.incidents),
        )
    )
    if attempt is None:
        return RedirectResponse(f"/{course.slug}/admin/attempts", status_code=303)

    answers_by_question = {answer.question_id: answer for answer in attempt.answers}
    rebuilt = pdf.build(attempt, answers_by_question, course)
    stored_sha256 = _sha256(attempt.pdf_bytes)
    current_sha256 = _sha256(rebuilt)
    is_match = bool(stored_sha256) and stored_sha256 == current_sha256
    message = (
        "Stored PDF matches current records."
        if is_match
        else "Stored PDF differs from current records."
    )

    attempt.last_pdf_audit_at = datetime.utcnow()
    attempt.last_pdf_audit_match = is_match
    attempt.last_pdf_audit_stored_sha256 = stored_sha256
    attempt.last_pdf_audit_current_sha256 = current_sha256
    attempt.last_pdf_audit_message = message

    _record_submission_audit(
        db,
        attempt=attempt,
        action="compare",
        status="ok" if is_match else "warning",
        actor=admin.email,
        message=message,
        stored_sha256=stored_sha256,
        current_sha256=current_sha256,
        is_match=is_match,
    )
    db.commit()
    logging_service.record(
        db,
        "PDF_COMPARED",
        f"Attempt {attempt.id}: {'match' if is_match else 'mismatch'}",
        request=request,
        payload={"attempt_id": attempt.id, "exam_id": attempt.exam_id, "is_match": is_match},
    )
    return RedirectResponse(
        f"/{course.slug}/admin/attempts/{attempt.id}?cmp={'match' if is_match else 'mismatch'}",
        status_code=303,
    )


@router.post("/attempts/{attempt_id}/mark-reviewed")
def mark_attempt_reviewed(
    attempt_id: int,
    request: Request,
    review_notes: str = Form(""),
    admin: Lecturer = Depends(require_admin),
    course: Course = Depends(require_admin_ready),
    db: Session = Depends(get_course_db),
):
    attempt = db.scalar(select(Attempt).where(Attempt.id == attempt_id))
    if attempt is None:
        return RedirectResponse(f"/{course.slug}/admin/attempts", status_code=303)

    attempt.reviewed_at = datetime.utcnow()
    attempt.reviewed_by = admin.email
    attempt.review_notes = (review_notes or "").strip()

    _record_submission_audit(
        db,
        attempt=attempt,
        action="review",
        status="ok",
        actor=admin.email,
        message="Submission marked as reviewed.",
        payload={"review_notes": attempt.review_notes},
    )
    db.commit()
    logging_service.record(
        db,
        "SUBMISSION_REVIEWED",
        f"Attempt {attempt.id}",
        request=request,
        payload={"attempt_id": attempt.id, "exam_id": attempt.exam_id},
    )
    return RedirectResponse(
        f"/{course.slug}/admin/attempts/{attempt.id}?reviewed=1",
        status_code=303,
    )


@router.get("/attempts/{attempt_id}/audit-report")
def attempt_audit_report(
    attempt_id: int,
    course: Course = Depends(require_admin_ready),
    db: Session = Depends(get_course_db),
):
    attempt = db.scalar(
        select(Attempt)
        .where(Attempt.id == attempt_id)
        .options(
            joinedload(Attempt.student),
            joinedload(Attempt.exam),
            selectinload(Attempt.audit_events),
        )
    )
    if attempt is None:
        return JSONResponse({"error": "Attempt not found."}, status_code=404)

    payload = {
        "attempt_id": attempt.id,
        "exam": {"id": attempt.exam.id, "title": attempt.exam.title},
        "student": {
            "id": attempt.student.id,
            "full_name": attempt.student.full_name,
            "computer_number": attempt.student.computer_number,
            "email": attempt.student.email,
        },
        "submitted_at": attempt.submitted_at.isoformat() if attempt.submitted_at else None,
        "review": {
            "reviewed_at": attempt.reviewed_at.isoformat() if attempt.reviewed_at else None,
            "reviewed_by": attempt.reviewed_by or "",
            "review_notes": attempt.review_notes or "",
        },
        "pdf_compare": {
            "at": attempt.last_pdf_audit_at.isoformat() if attempt.last_pdf_audit_at else None,
            "is_match": attempt.last_pdf_audit_match,
            "stored_sha256": attempt.last_pdf_audit_stored_sha256 or "",
            "current_sha256": attempt.last_pdf_audit_current_sha256 or "",
            "message": attempt.last_pdf_audit_message or "",
        },
        "history": [
            {
                "id": event.id,
                "at": event.at.isoformat() if event.at else None,
                "action": event.action,
                "status": event.status,
                "actor": event.actor,
                "message": event.message,
                "stored_sha256": event.stored_pdf_sha256,
                "current_sha256": event.current_pdf_sha256,
                "is_match": event.is_match,
                "payload": event.payload,
            }
            for event in sorted(attempt.audit_events, key=lambda item: item.id)
        ],
    }

    filename = f"audit_attempt_{attempt.id}.json"
    return Response(
        content=json.dumps(payload, indent=2, default=str),
        media_type="application/json",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.post("/exams/{exam_id}/settings")
def update_exam(
    exam_id: int,
    request: Request,
    title: str = Form(...),
    duration_minutes: int = Form(90),
    total_marks: int = Form(100),
    section_c_required: int = Form(2),
    instructions: str = Form(""),
    show_submission_pdf: str = Form(""),
    course: Course = Depends(require_admin_ready),
    db: Session = Depends(get_course_db),
):
    exam = db.get(Exam, exam_id)
    if exam is None:
        return RedirectResponse(f"/{course.slug}/admin", status_code=303)
    exam.title = title.strip()
    exam.duration_minutes = max(1, duration_minutes)
    exam.total_marks = total_marks
    exam.section_c_required = max(0, section_c_required)
    exam.instructions = instructions
    exam.show_submission_pdf = bool(show_submission_pdf)
    db.commit()
    logging_service.record(db, "EXAM_UPDATED", exam.title, request=request)
    return RedirectResponse(f"/{course.slug}/admin/exams/{exam_id}", status_code=303)


@router.post("/exams/{exam_id}/open")
def toggle_open(
    exam_id: int,
    request: Request,
    course: Course = Depends(require_admin_ready),
    db: Session = Depends(get_course_db),
):
    exam = db.get(Exam, exam_id)
    if exam is None:
        return RedirectResponse(f"/{course.slug}/admin", status_code=303)
    if not exam.is_open:
        # Opening an exam releases the paper to every verified student at once, so make
        # the obvious mistake impossible: an exam with no questions cannot be opened.
        if not exam.questions:
            return RedirectResponse(
                f"/{course.slug}/admin/exams/{exam_id}?error=empty", status_code=303
            )
        # Only one exam is live at a time; the sitting page always takes the open one.
        for other in db.scalars(select(Exam).where(Exam.is_open.is_(True))).all():
            other.is_open = False
    exam.is_open = not exam.is_open
    db.commit()
    logging_service.record(
        db,
        "EXAM_OPENED" if exam.is_open else "EXAM_CLOSED",
        exam.title,
        level="WARNING",
        request=request,
    )
    return RedirectResponse(f"/{course.slug}/admin/exams/{exam_id}", status_code=303)


@router.post("/exams/{exam_id}/questions")
def add_question(
    exam_id: int,
    request: Request,
    section: str = Form(...),
    prompt: str = Form(...),
    title: str = Form(""),
    marks: int = Form(0),
    options: str = Form(""),
    course: Course = Depends(require_admin_ready),
    db: Session = Depends(get_course_db),
):
    exam = db.get(Exam, exam_id)
    if exam is None:
        return RedirectResponse(f"/{course.slug}/admin", status_code=303)
    section = section.strip().upper()[:1]
    if section not in {"A", "B", "C"}:
        return RedirectResponse(
            f"/{course.slug}/admin/exams/{exam_id}?error=section", status_code=303
        )

    option_list = None
    if section == "A":
        option_list = [line.strip() for line in options.splitlines() if line.strip()]
        if len(option_list) < 2:
            return RedirectResponse(
                f"/{course.slug}/admin/exams/{exam_id}?error=options", status_code=303
            )

    highest = db.scalar(
        select(func.max(Question.order_index)).where(
            Question.exam_id == exam_id, Question.section == section
        )
    )
    question = Question(
        exam_id=exam_id,
        section=section,
        order_index=(highest or 0) + 1,
        title=title.strip(),
        prompt=prompt.strip(),
        options=option_list,
        marks=marks,
    )
    db.add(question)
    db.commit()
    logging_service.record(
        db, "QUESTION_ADDED", f"Section {section} in {exam.title}", request=request
    )
    return RedirectResponse(f"/{course.slug}/admin/exams/{exam_id}", status_code=303)


@router.post("/questions/{question_id}/delete")
def delete_question(
    question_id: int,
    request: Request,
    course: Course = Depends(require_admin_ready),
    db: Session = Depends(get_course_db),
):
    question = db.get(Question, question_id)
    if question is None:
        return RedirectResponse(f"/{course.slug}/admin", status_code=303)
    exam_id = question.exam_id
    if question.exam.attempts:
        # Removing a question that answers already point at would orphan them.
        return RedirectResponse(
            f"/{course.slug}/admin/exams/{exam_id}?error=inuse", status_code=303
        )
    db.delete(question)
    db.commit()
    logging_service.record(db, "QUESTION_DELETED", str(question_id), request=request)
    return RedirectResponse(f"/{course.slug}/admin/exams/{exam_id}", status_code=303)


@router.post("/exams/{exam_id}/import")
def import_questions(
    exam_id: int,
    request: Request,
    payload: str = Form(...),
    course: Course = Depends(require_admin_ready),
    db: Session = Depends(get_course_db),
):
    """Bulk load from the same quiz_data.json shape the desktop app used."""
    exam = db.get(Exam, exam_id)
    if exam is None:
        return RedirectResponse(f"/{course.slug}/admin", status_code=303)
    try:
        data = json.loads(payload)
    except json.JSONDecodeError:
        return RedirectResponse(
            f"/{course.slug}/admin/exams/{exam_id}?error=json", status_code=303
        )

    added = 0
    for index, item in enumerate(data.get("multiple_choice") or [], start=1):
        db.add(
            Question(
                exam_id=exam_id,
                section="A",
                order_index=index,
                prompt=str(item.get("question") or "").strip(),
                options=[str(option) for option in (item.get("options") or [])],
                marks=int(item.get("marks") or 0),
            )
        )
        added += 1
    for index, item in enumerate(data.get("short_answer") or [], start=1):
        db.add(
            Question(
                exam_id=exam_id, section="B", order_index=index, prompt=str(item).strip(), marks=0
            )
        )
        added += 1
    for index, item in enumerate(data.get("long_writeup") or [], start=1):
        db.add(
            Question(
                exam_id=exam_id,
                section="C",
                order_index=index,
                title=str(item.get("title") or "").strip(),
                prompt=str(item.get("prompt") or "").strip(),
                marks=int(item.get("marks") or 0),
            )
        )
        added += 1
    db.commit()
    logging_service.record(
        db, "QUESTIONS_IMPORTED", f"{added} questions into {exam.title}", request=request
    )
    return RedirectResponse(
        f"/{course.slug}/admin/exams/{exam_id}?imported={added}", status_code=303
    )


# ------------------------------------------------------------------------------ roster


@router.get("/students", response_class=HTMLResponse)
def students(
    request: Request,
    q: str = "",
    course: Course = Depends(require_admin_ready),
    db: Session = Depends(get_course_db),
):
    query = select(Student).order_by(Student.computer_number)
    if q:
        like = f"%{q.lower()}%"
        query = query.where(
            func.lower(Student.full_name).like(like)
            | func.lower(Student.email).like(like)
            | func.lower(Student.computer_number).like(like)
        )
    return templates.TemplateResponse(
        request, "admin/students.html", {"students": db.scalars(query).all(), "q": q}
    )


@router.post("/students/{student_id}/{action}")
def student_action(
    student_id: int,
    action: str,
    request: Request,
    course: Course = Depends(require_admin_ready),
    db: Session = Depends(get_course_db),
):
    student = db.get(Student, student_id)
    if student is None:
        return RedirectResponse(f"/{course.slug}/admin/students", status_code=303)
    if action == "resend_verification":
        if not student.is_verified:
            # Reuse the same verification flow students get at registration/resend.
            from app.routers.auth import send_verification_email

            send_verification_email(db, student, course, request)
            logging_service.record(
                db,
                "STUDENT_VERIFICATION_RESENT",
                f"{student.computer_number} {student.full_name}",
                student_id=student.id,
                request=request,
            )
        return RedirectResponse(f"/{course.slug}/admin/students", status_code=303)
    if action == "delete":
        if student.attempts:
            # An attempt carries submitted answers and proctoring evidence; deleting the
            # student out from under it would either orphan that record or, on a database
            # enforcing the foreign key, fail outright. Block it here with a clear reason
            # instead of either.
            return RedirectResponse(
                f"/{course.slug}/admin/students?error=inuse", status_code=303
            )
        label = f"{student.computer_number} {student.full_name}"
        db.delete(student)
        db.commit()
        logging_service.record(
            db, "STUDENT_DELETED", label, level="WARNING", request=request
        )
        return RedirectResponse(f"/{course.slug}/admin/students", status_code=303)
    if action == "approve":
        student.is_approved = True
    elif action == "block":
        student.is_blocked = True
    elif action == "unblock":
        student.is_blocked = False
    elif action == "verify":
        # Manual override for a student whose email genuinely will not arrive.
        student.is_verified = True
        student.verified_at = datetime.utcnow()
        student.verification_token = None
    db.commit()
    logging_service.record(
        db,
        f"STUDENT_{action.upper()}",
        f"{student.computer_number} {student.full_name}",
        level="WARNING",
        student_id=student.id,
        request=request,
    )
    return RedirectResponse(f"/{course.slug}/admin/students", status_code=303)


# ----------------------------------------------------------------------------- results


@router.get("/attempts", response_class=HTMLResponse)
def attempts(
    request: Request,
    flagged: int = 0,
    exam_id: int | None = None,
    course: Course = Depends(require_admin_ready),
    db: Session = Depends(get_course_db),
):
    query = (
        select(Attempt)
        .options(joinedload(Attempt.student), joinedload(Attempt.exam))
        .order_by(desc(Attempt.id))
    )
    if flagged:
        query = query.where(Attempt.flagged.is_(True))

    selected_exam: Exam | None = None
    if exam_id is not None:
        selected_exam = db.get(Exam, exam_id)
        if selected_exam is not None:
            query = query.where(Attempt.exam_id == exam_id)

    count_query = select(Attempt.exam_id, func.count(Attempt.id)).group_by(Attempt.exam_id)
    if flagged:
        count_query = count_query.where(Attempt.flagged.is_(True))
    per_exam_counts = {exam_key: count for exam_key, count in db.execute(count_query).all()}
    total_attempt_count = sum(per_exam_counts.values())

    exams = db.scalars(select(Exam).order_by(desc(Exam.id))).all()

    return templates.TemplateResponse(
        request,
        "admin/attempts.html",
        {
            "attempts": db.scalars(query).all(),
            "flagged_only": bool(flagged),
            "exams": exams,
            "per_exam_counts": per_exam_counts,
            "total_attempt_count": total_attempt_count,
            "current_exam_id": selected_exam.id if selected_exam is not None else None,
        },
    )


@router.get("/attempts/{attempt_id}", response_class=HTMLResponse)
def attempt_detail(
    attempt_id: int,
    request: Request,
    course: Course = Depends(require_admin_ready),
    db: Session = Depends(get_course_db),
):
    # Explicit eager-loading to prevent lazy-loading issues - loads student and exam
    # up front in the correct session context.
    attempt = db.scalar(
        select(Attempt)
        .where(Attempt.id == attempt_id)
        .options(
            joinedload(Attempt.student),
            joinedload(Attempt.exam),
            selectinload(Attempt.audit_events),
        )
    )

    if attempt is None:
        return RedirectResponse(f"/{course.slug}/admin/attempts", status_code=303)
    answers = {answer.question_id: answer for answer in attempt.answers}
    questions = sorted(attempt.exam.questions, key=lambda q: (q.section, q.order_index))
    return templates.TemplateResponse(
        request,
        "admin/attempt.html",
        {
            "attempt": attempt,
            "questions": questions,
            "answers": answers,
            "remaining": proctor.remaining_seconds(attempt),
            "audit_events": sorted(
                list(attempt.audit_events), key=lambda event: event.id, reverse=True
            )[:25],
        },
    )


@router.get("/attempts/{attempt_id}/pdf")
def attempt_pdf(
    attempt_id: int,
    course: Course = Depends(require_admin_ready),
    db: Session = Depends(get_course_db),
):
    attempt = db.scalar(select(Attempt).where(Attempt.id == attempt_id))
    if attempt is None or not attempt.pdf_bytes:
        return JSONResponse({"error": "No PDF stored for this attempt."}, status_code=404)
    return Response(
        content=attempt.pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": f'inline; filename="{attempt.pdf_filename}"'},
    )


@router.get("/snapshots/{snapshot_id}")
def snapshot_image(
    snapshot_id: int,
    course: Course = Depends(require_admin_ready),
    db: Session = Depends(get_course_db),
):
    snapshot = db.scalar(select(Snapshot).where(Snapshot.id == snapshot_id))
    if snapshot is None:
        return JSONResponse({"error": "Not found."}, status_code=404)
    return Response(content=snapshot.image, media_type=snapshot.mime)


# -------------------------------------------------------------------------------- logs


@router.get("/logs", response_class=HTMLResponse)
def logs(
    request: Request,
    q: str = "",
    level: str = "",
    limit: int = 200,
    course: Course = Depends(require_admin_ready),
    db: Session = Depends(get_course_db),
):
    query = select(AppLog).order_by(desc(AppLog.id)).limit(min(max(limit, 10), 1000))
    if q:
        like = f"%{q.lower()}%"
        query = query.where(
            func.lower(AppLog.event_type).like(like) | func.lower(AppLog.message).like(like)
        )
    if level:
        query = query.where(AppLog.level == level.upper())
    total = db.scalar(select(func.count(AppLog.id))) or 0
    return templates.TemplateResponse(
        request,
        "admin/logs.html",
        {"logs": db.scalars(query).all(), "q": q, "level": level, "limit": limit, "total": total},
    )
