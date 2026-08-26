"""Sitting the test: starting an attempt, saving answers, reporting incidents, submitting."""

import base64
from datetime import datetime

from fastapi import APIRouter, Body, Depends, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import logging_service, pdf, proctor, vision
from app.config import settings
from app.deps import get_course_db, require_course_ready, require_student, templates
from app.mailer import Attachment, Message, send
from app.models_course import Answer, Attempt, Exam, Question, Snapshot, Student
from app.models_platform import Lecturer
from app.security import deadline_from

router = APIRouter()


def _open_exam(db: Session) -> Exam | None:
    return db.scalar(select(Exam).where(Exam.is_open.is_(True)).order_by(Exam.id.desc()))


def _answers_map(db: Session, attempt: Attempt) -> dict[int, Answer]:
    return {answer.question_id: answer for answer in attempt.answers}


def _questions(exam: Exam) -> list[Question]:
    return sorted(exam.questions, key=lambda q: (q.section, q.order_index))


@router.get("/", response_class=HTMLResponse)
def dashboard(
    request: Request,
    student: Student = Depends(require_student),
    lecturer: Lecturer = Depends(require_course_ready),
    db: Session = Depends(get_course_db),
):
    exam = _open_exam(db)
    attempt = None
    if exam:
        attempt = db.scalar(
            select(Attempt).where(
                Attempt.exam_id == exam.id, Attempt.student_id == student.id
            )
        )
    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {"student": student, "exam": exam, "attempt": attempt},
    )


@router.post("/start")
def start(
    request: Request,
    student: Student = Depends(require_student),
    lecturer: Lecturer = Depends(require_course_ready),
    db: Session = Depends(get_course_db),
):
    exam = _open_exam(db)
    if exam is None:
        return RedirectResponse(f"/{lecturer.slug}/", status_code=303)

    attempt = db.scalar(
        select(Attempt).where(Attempt.exam_id == exam.id, Attempt.student_id == student.id)
    )
    if attempt is None:
        started = datetime.utcnow()
        attempt = Attempt(
            exam_id=exam.id,
            student_id=student.id,
            started_at=started,
            deadline_at=deadline_from(started, exam.duration_minutes),
        )
        db.add(attempt)
        db.commit()
        db.refresh(attempt)
        logging_service.record(
            db,
            "ATTEMPT_STARTED",
            f"{student.full_name} started {exam.title}",
            student_id=student.id,
            attempt_id=attempt.id,
            request=request,
        )
    else:
        proctor.record_incident(
            db, attempt, "RECONNECTED", "The test page was reopened.", source="browser"
        )
    return RedirectResponse(f"/{lecturer.slug}/sit", status_code=303)


@router.get("/sit", response_class=HTMLResponse)
def sit(
    request: Request,
    student: Student = Depends(require_student),
    lecturer: Lecturer = Depends(require_course_ready),
    db: Session = Depends(get_course_db),
):
    exam = _open_exam(db)
    if exam is None:
        return RedirectResponse(f"/{lecturer.slug}/", status_code=303)
    attempt = db.scalar(
        select(Attempt).where(Attempt.exam_id == exam.id, Attempt.student_id == student.id)
    )
    if attempt is None:
        return RedirectResponse(f"/{lecturer.slug}/", status_code=303)
    if attempt.is_locked:
        return RedirectResponse(f"/{lecturer.slug}/submitted", status_code=303)

    proctor.touch(attempt)
    db.commit()

    questions = _questions(exam)
    answers = _answers_map(db, attempt)
    payload = [
        {
            "id": question.id,
            "section": question.section,
            "title": question.title,
            "prompt": question.prompt,
            "options": question.options or [],
            "marks": question.marks,
            "value": (answers.get(question.id).value if answers.get(question.id) else ""),
            "selected": bool(answers.get(question.id).selected) if answers.get(question.id) else False,
        }
        for question in questions
    ]
    return templates.TemplateResponse(
        request,
        "sit.html",
        {
            "student": student,
            "exam": exam,
            "attempt": attempt,
            "questions_json": payload,
            "settings_json": proctor.client_settings(),
            "remaining": proctor.remaining_seconds(attempt),
            "current_question": attempt.current_question or 0,
        },
    )


@router.post("/api/save")
def save(
    request: Request,
    body: dict = Body(...),
    student: Student = Depends(require_student),
    lecturer: Lecturer = Depends(require_course_ready),
    db: Session = Depends(get_course_db),
):
    attempt = _live_attempt(db, student)
    if attempt is None:
        return JSONResponse({"error": "No live attempt."}, status_code=409)
    if attempt.is_locked:
        return JSONResponse({"error": "This paper is already submitted."}, status_code=409)

    proctor.touch(attempt)
    attempt.current_question = int(body.get("current_question") or 0)

    existing = _answers_map(db, attempt)
    valid_ids = {question.id for question in attempt.exam.questions}
    for item in body.get("answers") or []:
        try:
            question_id = int(item.get("question_id"))
        except (TypeError, ValueError):
            continue
        if question_id not in valid_ids:
            continue
        value = str(item.get("value") or "")[:20000]
        selected = bool(item.get("selected"))
        answer = existing.get(question_id)
        if answer is None:
            answer = Answer(attempt_id=attempt.id, question_id=question_id)
            db.add(answer)
            existing[question_id] = answer
        answer.value = value
        answer.selected = selected

    db.commit()
    return {"ok": True, **proctor.status_payload(attempt)}


@router.get("/api/status")
def status(
    request: Request,
    student: Student = Depends(require_student),
    lecturer: Lecturer = Depends(require_course_ready),
    db: Session = Depends(get_course_db),
):
    attempt = _live_attempt(db, student)
    if attempt is None:
        return JSONResponse({"error": "No live attempt."}, status_code=409)
    proctor.touch(attempt)
    db.commit()
    return proctor.status_payload(attempt)


@router.post("/api/incident")
def incident(
    request: Request,
    body: dict = Body(...),
    student: Student = Depends(require_student),
    lecturer: Lecturer = Depends(require_course_ready),
    db: Session = Depends(get_course_db),
):
    attempt = _live_attempt(db, student)
    if attempt is None:
        return JSONResponse({"error": "No live attempt."}, status_code=409)
    if attempt.is_locked:
        return JSONResponse({"ok": True, "locked": True})

    category = str(body.get("category") or "UNKNOWN").upper()
    detail = str(body.get("detail") or "")
    entry = proctor.record_incident(db, attempt, category, detail, source="browser")

    snapshot = None
    image_b64 = body.get("snapshot")
    if settings.snapshots_enabled and image_b64 and category in proctor.ALERTABLE:
        snapshot = _store_snapshot(db, attempt, entry.id, image_b64)

    logging_service.record(
        db,
        f"INCIDENT_{category}",
        detail,
        level="WARNING",
        student_id=student.id,
        attempt_id=attempt.id,
        request=request,
        payload={"counted": entry.counted, "snapshot": bool(snapshot)},
    )

    if snapshot is not None and proctor.should_email_alert(attempt, category):
        attempt.last_alert_email_at = datetime.utcnow()
        db.commit()
        _email_alert(db, attempt, entry, snapshot, lecturer)

    return {"ok": True, **proctor.status_payload(attempt)}


@router.post("/api/submit")
def submit(
    request: Request,
    body: dict = Body(default={}),
    student: Student = Depends(require_student),
    lecturer: Lecturer = Depends(require_course_ready),
    db: Session = Depends(get_course_db),
):
    attempt = _live_attempt(db, student)
    if attempt is None:
        return JSONResponse({"error": "No live attempt."}, status_code=409)
    if attempt.is_locked:
        return JSONResponse(
            {"error": "This paper has already been submitted.", "locked": True}, status_code=409
        )

    auto = bool(body.get("auto"))
    reason = str(body.get("reason") or "")

    if not auto:
        required = attempt.exam.section_c_required
        selected = sum(
            1
            for answer in attempt.answers
            if answer.selected and answer.question and answer.question.section == "C"
        )
        if required and selected != required:
            return JSONResponse(
                {
                    "error": (
                        f"Select exactly {required} Section C question(s) before submitting. "
                        f"You have {selected} selected."
                    )
                },
                status_code=400,
            )

    proctor.touch(attempt)
    attempt.submitted_at = datetime.utcnow()
    attempt.is_locked = True
    attempt.submission_mode = "automatic" if auto else "manual"
    attempt.auto_submit_reason = reason[:1000]
    db.commit()
    
    # SECURITY FIX: Refresh attempt with explicit eager-loading to prevent lazy-loading issues
    # This ensures student details are loaded from the correct session context
    from sqlalchemy.orm import joinedload
    db.expire(attempt)  # Clear potentially stale data
    attempt = db.scalar(
        select(Attempt)
        .where(Attempt.id == attempt.id)
        .options(
            joinedload(Attempt.student),
            joinedload(Attempt.exam).joinedload(Exam.questions)
        )
    )

    document = pdf.build(attempt, _answers_map(db, attempt), lecturer)
    attempt.pdf_bytes = document
    attempt.pdf_filename = pdf.filename_for(attempt, lecturer)
    db.commit()

    logging_service.record(
        db,
        "SUBMITTED",
        f"{student.full_name} submitted {attempt.exam.title} ({attempt.submission_mode})",
        student_id=student.id,
        attempt_id=attempt.id,
        request=request,
        payload={"strikes": attempt.strike_count, "flagged": attempt.flagged},
    )
    _email_submission(db, attempt, document, lecturer)
    return {"ok": True, "locked": True, "redirect": f"/{lecturer.slug}/submitted"}


@router.get("/submitted", response_class=HTMLResponse)
def submitted(
    request: Request,
    student: Student = Depends(require_student),
    lecturer: Lecturer = Depends(require_course_ready),
    db: Session = Depends(get_course_db),
):
    # SECURITY FIX: Order by submitted_at DESC (most recent) instead of ID DESC
    # This ensures we show the MOST RECENT submission, not just the highest ID
    # Prevents showing wrong exam's submission if student has multiple submitted attempts
    attempt = db.scalar(
        select(Attempt)
        .where(Attempt.student_id == student.id, Attempt.is_locked.is_(True))
        .order_by(Attempt.submitted_at.desc())
    )
    return templates.TemplateResponse(
        request, "submitted.html", {"student": student, "attempt": attempt}
    )


@router.get("/my-submission.pdf")
def my_submission(
    student: Student = Depends(require_student),
    lecturer: Lecturer = Depends(require_course_ready),
    db: Session = Depends(get_course_db),
):
    # SECURITY FIX: Order by submitted_at DESC (most recent) instead of ID DESC
    # This ensures we serve the MOST RECENT submission, not just the highest ID
    # Prevents serving wrong exam's PDF if student has multiple submitted attempts
    attempt = db.scalar(
        select(Attempt)
        .where(Attempt.student_id == student.id, Attempt.is_locked.is_(True))
        .order_by(Attempt.submitted_at.desc())
    )
    if attempt is None or not attempt.pdf_bytes:
        return JSONResponse({"error": "No submission found."}, status_code=404)
    return Response(
        content=attempt.pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": f'inline; filename="{attempt.pdf_filename}"'},
    )


# --------------------------------------------------------------------------- helpers


def _live_attempt(db: Session, student: Student) -> Attempt | None:
    exam = _open_exam(db)
    if exam is None:
        return None
    return db.scalar(
        select(Attempt).where(Attempt.exam_id == exam.id, Attempt.student_id == student.id)
    )


def _store_snapshot(db: Session, attempt: Attempt, incident_id: int, image_b64: str) -> Snapshot | None:
    if len(attempt.snapshots) >= settings.snapshot_max_per_attempt:
        return None
    try:
        raw = image_b64.split(",", 1)[-1]
        image = base64.b64decode(raw, validate=False)
    except Exception:
        return None
    # A 480px JPEG should be tens of kilobytes. Anything far larger is not our frame.
    if not image or len(image) > 2_000_000:
        return None

    verdict, confidence = vision.check_snapshot(image)
    snapshot = Snapshot(
        attempt_id=attempt.id,
        incident_id=incident_id,
        image=image,
        mime="image/jpeg",
        byte_size=len(image),
        server_verdict=verdict,
        server_confidence=confidence,
    )
    db.add(snapshot)
    db.commit()
    db.refresh(snapshot)
    return snapshot


def _student_block(attempt: Attempt) -> str:
    student = attempt.student
    return (
        f"Student:          {student.full_name}\n"
        f"Computer number:  {student.computer_number}\n"
        f"Email:            {student.email}\n"
        f"Exam:             {attempt.exam.title}\n"
        f"Attempt started:  {attempt.started_at.strftime('%Y-%m-%d %H:%M:%S')} UTC\n"
    )


def _email_alert(
    db: Session, attempt: Attempt, incident, snapshot: Snapshot, lecturer: Lecturer
) -> None:
    minutes, seconds = divmod(int(incident.elapsed_seconds or 0), 60)
    verdict_line = {
        "phone_detected": "The server re-checked the image and also detected a phone-like device.",
        "no_phone": "The server re-checked the image and did NOT confirm a phone. Treat with caution.",
        "not_checked": "No server-side re-check was performed on this image.",
    }.get(snapshot.server_verdict, "")

    body = (
        f"A proctoring incident was recorded during {lecturer.brand}.\n\n"
        f"{_student_block(attempt)}"
        f"\nIncident:         {incident.label}\n"
        f"At:               {incident.occurred_at.strftime('%Y-%m-%d %H:%M:%S')} UTC "
        f"({minutes:02d}:{seconds:02d} into the test)\n"
        f"Detail:           {incident.detail}\n"
        f"Strikes so far:   {attempt.strike_count} of {settings.strike_flag_after}\n"
        f"Flagged:          {'YES' if attempt.flagged else 'not yet'}\n\n"
        f"{verdict_line}\n\n"
        "The evidence snapshot is attached. A recorded incident is a prompt to look, not a\n"
        "finding of misconduct - cameras misread ordinary movement. Further alerts for this\n"
        f"candidate are held for {settings.snapshot_alert_min_interval_seconds // 60} minutes.\n\n"
        f"Review the full log: {settings.base_url.rstrip('/')}/{lecturer.slug}/admin/attempts/{attempt.id}\n"
    )
    delivered = send(
        Message(
            to=lecturer.email,
            subject=(
                f"[{lecturer.brand}] {incident.label} - "
                f"{attempt.student.computer_number} {attempt.student.full_name}"
            ),
            body=body,
            attachments=[
                Attachment(
                    filename=f"incident_{incident.id}.jpg",
                    content=snapshot.image,
                    mime_type="image/jpeg",
                )
            ],
        ),
        lecturer,
    )
    logging_service.record(
        db,
        "ALERT_EMAIL_SENT" if delivered else "ALERT_EMAIL_FAILED",
        f"{incident.label} for {attempt.student.computer_number}",
        level="INFO" if delivered else "ERROR",
        student_id=attempt.student_id,
        attempt_id=attempt.id,
    )


def _email_submission(db: Session, attempt: Attempt, document: bytes, lecturer: Lecturer) -> None:
    incidents = list(attempt.incidents)
    counted = sum(1 for incident in incidents if incident.counted)
    lines = [
        f"{incident.occurred_at.strftime('%H:%M:%S')}  "
        f"{'[counted] ' if incident.counted else '          '}{incident.label}"
        for incident in incidents[:40]
    ]
    log_text = "\n".join(lines) if lines else "  none recorded"
    if len(incidents) > 40:
        log_text += f"\n  ... and {len(incidents) - 40} more, see the admin panel"

    body = (
        f"A submission has been received for {lecturer.brand}.\n\n"
        f"{_student_block(attempt)}"
        f"Submitted:        {attempt.submitted_at.strftime('%Y-%m-%d %H:%M:%S')} UTC\n"
        f"Submission mode:  {attempt.submission_mode}\n"
        f"{('Reason:           ' + attempt.auto_submit_reason + chr(10)) if attempt.auto_submit_reason else ''}"
        f"Incidents:        {counted} counted of {len(incidents)} recorded\n"
        f"Flagged:          {'YES - review before marking' if attempt.flagged else 'no'}\n"
        f"Snapshots:        {len(attempt.snapshots)}\n\n"
        "Incident log:\n"
        f"{log_text}\n\n"
        f"Full record: {settings.base_url.rstrip('/')}/{lecturer.slug}/admin/attempts/{attempt.id}\n"
    )
    delivered = send(
        Message(
            to=lecturer.email,
            subject=(
                f"[{lecturer.brand}] Submission - "
                f"{attempt.student.computer_number} {attempt.student.full_name}"
                f"{' - FLAGGED' if attempt.flagged else ''}"
            ),
            body=body,
            attachments=[Attachment(filename=attempt.pdf_filename, content=document)],
        ),
        lecturer,
    )
    logging_service.record(
        db,
        "SUBMISSION_EMAIL_SENT" if delivered else "SUBMISSION_EMAIL_FAILED",
        f"Submission PDF for {attempt.student.computer_number} to {lecturer.email}",
        level="INFO" if delivered else "ERROR",
        student_id=attempt.student_id,
        attempt_id=attempt.id,
    )
