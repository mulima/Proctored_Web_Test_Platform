"""Admin panel: exam and question setup, roster, results, evidence, logs - and,
new here, the course's own setup: database connection and branding.

The lecturer's identity and password now live in the platform database
(app/models_platform.py's Lecturer), not in the course database this router
otherwise reads and writes via get_course_db. Every route below except
/setup itself additionally requires the course database to be connected and
validated (require_admin_ready) - see app/deps.py.
"""

import json
from datetime import datetime

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from sqlalchemy import desc, func, select
from sqlalchemy.orm import Session

from app import logging_service, proctor, vision
from app.config import settings
from app.db import get_db
from app.deps import get_course_db, get_lecturer, require_admin, require_admin_ready, templates
from app.models_course import AppLog, Attempt, Exam, Question, Snapshot, Student
from app.models_platform import Lecturer
from app.security import set_session_cookie, verify_password
from app.tenant_crypto import CredentialEncryptionError, encrypt
from app.tenant_db import engine_for_url, forget, validate_schema

router = APIRouter(prefix="/admin")


@router.get("/login", response_class=HTMLResponse)
def login_form(request: Request, lecturer: Lecturer = Depends(get_lecturer)):
    return templates.TemplateResponse(
        request, "admin/login.html", {"errors": [], "prefill_email": lecturer.email}
    )


@router.post("/login")
def login(
    request: Request,
    email: str = Form(...),
    password: str = Form(...),
    lecturer: Lecturer = Depends(get_lecturer),
    platform_db: Session = Depends(get_db),
):
    email = email.strip().lower()
    if email != lecturer.email or not verify_password(password, lecturer.password_hash):
        logging_service.record_platform(
            platform_db,
            "ADMIN_LOGIN_FAILED",
            email,
            level="WARNING",
            lecturer_id=lecturer.id,
            request=request,
        )
        return templates.TemplateResponse(
            request,
            "admin/login.html",
            {"errors": ["Email or password is incorrect."], "prefill_email": email},
            status_code=401,
        )
    if not lecturer.is_verified:
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
    logging_service.record_platform(
        platform_db, "ADMIN_LOGIN", email, lecturer_id=lecturer.id, request=request
    )
    response = RedirectResponse(f"/{lecturer.slug}/admin", status_code=303)
    set_session_cookie(response, role="admin", slug=lecturer.slug, id=lecturer.id)
    return response


# ------------------------------------------------------------------------------ setup


@router.get("/setup", response_class=HTMLResponse)
def setup_form(
    request: Request, lecturer: Lecturer = Depends(require_admin)
):
    return templates.TemplateResponse(
        request, "admin/setup.html", _setup_context(lecturer, [])
    )


def _setup_context(lecturer: Lecturer, errors: list[str]) -> dict:
    return {
        "errors": errors,
        "lecturer": lecturer,
        "has_database": bool(lecturer.database_url_encrypted),
        "has_smtp_password": bool(lecturer.smtp_password_encrypted),
        "has_resend_key": bool(lecturer.resend_api_key_encrypted),
    }


@router.post("/setup/database", response_class=HTMLResponse)
def setup_database(
    request: Request,
    database_url: str = Form(""),
    lecturer: Lecturer = Depends(require_admin),
    platform_db: Session = Depends(get_db),
):
    errors: list[str] = []
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
                    lecturer.database_url_encrypted = encrypted
                    lecturer.database_ready = True
                    forget(lecturer.id)  # drop any previously cached engine for this lecturer
                    logging_service.record_platform(
                        platform_db,
                        "LECTURER_DATABASE_CONNECTED",
                        lecturer.slug,
                        lecturer_id=lecturer.id,
                        request=request,
                    )

    platform_db.commit()

    if errors:
        return templates.TemplateResponse(
            request, "admin/setup.html", _setup_context(lecturer, errors), status_code=400
        )
    destination = f"/{lecturer.slug}/admin" if lecturer.database_ready else f"/{lecturer.slug}/admin/setup?saved=1"
    return RedirectResponse(destination, status_code=303)


@router.post("/setup/branding", response_class=HTMLResponse)
def setup_branding(
    request: Request,
    course_code: str = Form(""),
    course_title: str = Form(""),
    institution: str = Form(""),
    lecturer: Lecturer = Depends(require_admin),
    platform_db: Session = Depends(get_db),
):
    lecturer.course_code = course_code.strip()
    lecturer.course_title = course_title.strip()
    lecturer.institution = institution.strip()
    platform_db.commit()
    return RedirectResponse(f"/{lecturer.slug}/admin/setup?saved=1", status_code=303)


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
    lecturer: Lecturer = Depends(require_admin),
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
    if mail_backend == "smtp" and not (smtp_host.strip() or lecturer.smtp_host):
        errors.append("SMTP host is required when the mail backend is smtp.")
    if mail_backend == "resend" and not (resend_api_key.strip() or lecturer.resend_api_key_encrypted):
        errors.append("A Resend API key is required when the mail backend is resend.")

    if errors:
        return templates.TemplateResponse(
            request, "admin/setup.html", _setup_context(lecturer, errors), status_code=400
        )

    lecturer.mail_backend = mail_backend
    lecturer.mail_from = mail_from.strip()
    lecturer.smtp_host = smtp_host.strip()
    lecturer.smtp_port = int(smtp_port) if smtp_port.strip().isdigit() else None
    lecturer.smtp_username = smtp_username.strip()
    lecturer.smtp_use_tls = bool(smtp_use_tls)
    if smtp_password.strip():
        lecturer.smtp_password_encrypted = encrypt(smtp_password.strip())
    if resend_api_key.strip():
        lecturer.resend_api_key_encrypted = encrypt(resend_api_key.strip())
    platform_db.commit()
    logging_service.record_platform(
        platform_db, "LECTURER_EMAIL_UPDATED", lecturer.slug, lecturer_id=lecturer.id, request=request
    )
    return RedirectResponse(f"/{lecturer.slug}/admin/setup?saved=1", status_code=303)


@router.get("", response_class=HTMLResponse)
def home(
    request: Request,
    lecturer: Lecturer = Depends(require_admin_ready),
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


@router.post("/exams")
def create_exam(
    request: Request,
    title: str = Form(...),
    duration_minutes: int = Form(90),
    total_marks: int = Form(100),
    section_c_required: int = Form(2),
    lecturer: Lecturer = Depends(require_admin_ready),
    db: Session = Depends(get_course_db),
):
    exam = Exam(
        code=lecturer.course_code,
        title=title.strip(),
        duration_minutes=max(1, duration_minutes),
        total_marks=total_marks,
        section_c_required=max(0, section_c_required),
        is_open=False,
    )
    db.add(exam)
    db.commit()
    logging_service.record(db, "EXAM_CREATED", exam.title, request=request)
    return RedirectResponse(f"/{lecturer.slug}/admin/exams/{exam.id}", status_code=303)


@router.get("/exams/{exam_id}", response_class=HTMLResponse)
def exam_detail(
    exam_id: int,
    request: Request,
    lecturer: Lecturer = Depends(require_admin_ready),
    db: Session = Depends(get_course_db),
):
    exam = db.get(Exam, exam_id)
    if exam is None:
        return RedirectResponse(f"/{lecturer.slug}/admin", status_code=303)
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


@router.post("/exams/{exam_id}/settings")
def update_exam(
    exam_id: int,
    request: Request,
    title: str = Form(...),
    duration_minutes: int = Form(90),
    total_marks: int = Form(100),
    section_c_required: int = Form(2),
    instructions: str = Form(""),
    lecturer: Lecturer = Depends(require_admin_ready),
    db: Session = Depends(get_course_db),
):
    exam = db.get(Exam, exam_id)
    if exam is None:
        return RedirectResponse(f"/{lecturer.slug}/admin", status_code=303)
    exam.title = title.strip()
    exam.duration_minutes = max(1, duration_minutes)
    exam.total_marks = total_marks
    exam.section_c_required = max(0, section_c_required)
    exam.instructions = instructions
    db.commit()
    logging_service.record(db, "EXAM_UPDATED", exam.title, request=request)
    return RedirectResponse(f"/{lecturer.slug}/admin/exams/{exam_id}", status_code=303)


@router.post("/exams/{exam_id}/open")
def toggle_open(
    exam_id: int,
    request: Request,
    lecturer: Lecturer = Depends(require_admin_ready),
    db: Session = Depends(get_course_db),
):
    exam = db.get(Exam, exam_id)
    if exam is None:
        return RedirectResponse(f"/{lecturer.slug}/admin", status_code=303)
    if not exam.is_open:
        # Opening an exam releases the paper to every verified student at once, so make
        # the obvious mistake impossible: an exam with no questions cannot be opened.
        if not exam.questions:
            return RedirectResponse(
                f"/{lecturer.slug}/admin/exams/{exam_id}?error=empty", status_code=303
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
    return RedirectResponse(f"/{lecturer.slug}/admin/exams/{exam_id}", status_code=303)


@router.post("/exams/{exam_id}/questions")
def add_question(
    exam_id: int,
    request: Request,
    section: str = Form(...),
    prompt: str = Form(...),
    title: str = Form(""),
    marks: int = Form(0),
    options: str = Form(""),
    lecturer: Lecturer = Depends(require_admin_ready),
    db: Session = Depends(get_course_db),
):
    exam = db.get(Exam, exam_id)
    if exam is None:
        return RedirectResponse(f"/{lecturer.slug}/admin", status_code=303)
    section = section.strip().upper()[:1]
    if section not in {"A", "B", "C"}:
        return RedirectResponse(
            f"/{lecturer.slug}/admin/exams/{exam_id}?error=section", status_code=303
        )

    option_list = None
    if section == "A":
        option_list = [line.strip() for line in options.splitlines() if line.strip()]
        if len(option_list) < 2:
            return RedirectResponse(
                f"/{lecturer.slug}/admin/exams/{exam_id}?error=options", status_code=303
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
    return RedirectResponse(f"/{lecturer.slug}/admin/exams/{exam_id}", status_code=303)


@router.post("/questions/{question_id}/delete")
def delete_question(
    question_id: int,
    request: Request,
    lecturer: Lecturer = Depends(require_admin_ready),
    db: Session = Depends(get_course_db),
):
    question = db.get(Question, question_id)
    if question is None:
        return RedirectResponse(f"/{lecturer.slug}/admin", status_code=303)
    exam_id = question.exam_id
    if question.exam.attempts:
        # Removing a question that answers already point at would orphan them.
        return RedirectResponse(
            f"/{lecturer.slug}/admin/exams/{exam_id}?error=inuse", status_code=303
        )
    db.delete(question)
    db.commit()
    logging_service.record(db, "QUESTION_DELETED", str(question_id), request=request)
    return RedirectResponse(f"/{lecturer.slug}/admin/exams/{exam_id}", status_code=303)


@router.post("/exams/{exam_id}/import")
def import_questions(
    exam_id: int,
    request: Request,
    payload: str = Form(...),
    lecturer: Lecturer = Depends(require_admin_ready),
    db: Session = Depends(get_course_db),
):
    """Bulk load from the same quiz_data.json shape the desktop app used."""
    exam = db.get(Exam, exam_id)
    if exam is None:
        return RedirectResponse(f"/{lecturer.slug}/admin", status_code=303)
    try:
        data = json.loads(payload)
    except json.JSONDecodeError:
        return RedirectResponse(
            f"/{lecturer.slug}/admin/exams/{exam_id}?error=json", status_code=303
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
        f"/{lecturer.slug}/admin/exams/{exam_id}?imported={added}", status_code=303
    )


# ------------------------------------------------------------------------------ roster


@router.get("/students", response_class=HTMLResponse)
def students(
    request: Request,
    q: str = "",
    lecturer: Lecturer = Depends(require_admin_ready),
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
    lecturer: Lecturer = Depends(require_admin_ready),
    db: Session = Depends(get_course_db),
):
    student = db.get(Student, student_id)
    if student is None:
        return RedirectResponse(f"/{lecturer.slug}/admin/students", status_code=303)
    if action == "delete":
        if student.attempts:
            # An attempt carries submitted answers and proctoring evidence; deleting the
            # student out from under it would either orphan that record or, on a database
            # enforcing the foreign key, fail outright. Block it here with a clear reason
            # instead of either.
            return RedirectResponse(
                f"/{lecturer.slug}/admin/students?error=inuse", status_code=303
            )
        label = f"{student.computer_number} {student.full_name}"
        db.delete(student)
        db.commit()
        logging_service.record(
            db, "STUDENT_DELETED", label, level="WARNING", request=request
        )
        return RedirectResponse(f"/{lecturer.slug}/admin/students", status_code=303)
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
    return RedirectResponse(f"/{lecturer.slug}/admin/students", status_code=303)


# ----------------------------------------------------------------------------- results


@router.get("/attempts", response_class=HTMLResponse)
def attempts(
    request: Request,
    flagged: int = 0,
    lecturer: Lecturer = Depends(require_admin_ready),
    db: Session = Depends(get_course_db),
):
    query = select(Attempt).order_by(desc(Attempt.id))
    if flagged:
        query = query.where(Attempt.flagged.is_(True))
    return templates.TemplateResponse(
        request,
        "admin/attempts.html",
        {"attempts": db.scalars(query).all(), "flagged_only": bool(flagged)},
    )


@router.get("/attempts/{attempt_id}", response_class=HTMLResponse)
def attempt_detail(
    attempt_id: int,
    request: Request,
    lecturer: Lecturer = Depends(require_admin_ready),
    db: Session = Depends(get_course_db),
):
    attempt = db.get(Attempt, attempt_id)
    if attempt is None:
        return RedirectResponse(f"/{lecturer.slug}/admin/attempts", status_code=303)
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
        },
    )


@router.get("/attempts/{attempt_id}/pdf")
def attempt_pdf(
    attempt_id: int,
    lecturer: Lecturer = Depends(require_admin_ready),
    db: Session = Depends(get_course_db),
):
    attempt = db.get(Attempt, attempt_id)
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
    lecturer: Lecturer = Depends(require_admin_ready),
    db: Session = Depends(get_course_db),
):
    snapshot = db.get(Snapshot, snapshot_id)
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
    lecturer: Lecturer = Depends(require_admin_ready),
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
