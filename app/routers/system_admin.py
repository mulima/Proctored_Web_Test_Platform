"""Platform operator backend - not a lecturer, not scoped to any one course.

A single, env-var-configured account (app/config.py's system_admin_username /
system_admin_password_hash) that can see aggregate numbers and a roster across
every lecturer and course on this deployment. Deliberately not a database row -
one operator, no self-service signup, no password-reset flow to build or secure.
Its session cookie is kept entirely separate from a lecturer's or student's - see
app/security.py's set_system_admin_cookie.

Exam/question counts live in each course's OWN database, not here - see
app/tenant_db.py's fetch_course_exam_summary, used the same bounded-timeout,
one-course-failing-doesn't-break-the-page way the cross-course "all my students"
dashboard already does.
"""

from collections import Counter
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app import logging_service
from app.config import settings
from app.db import get_db
from app.deps import require_system_admin, templates
from app.models_platform import Course, Lecturer, PageVisit, PlatformLog
from app.monitoring import repeated_platform_event
from app.security import (
    clear_system_admin_cookie,
    set_system_admin_cookie,
    tokens_match,
    verify_password,
)
from app.tenant_db import fetch_course_exam_summary, fetch_course_students

router = APIRouter(prefix="/administrator")

_DAYS_BACK = 30


def _day_buckets(timestamps: list[datetime], days: int = _DAYS_BACK) -> list[tuple[str, int]]:
    """[(YYYY-MM-DD, count), ...] for the last `days` days, oldest first, zero-filled -
    done in Python rather than a SQL date-trunc so this behaves identically on
    SQLite (local dev) and Postgres (deployed) with no dialect-specific function."""
    today = datetime.utcnow().date()
    counts = Counter(ts.date() for ts in timestamps)
    return [
        ((today - timedelta(days=offset)).isoformat(), counts.get(today - timedelta(days=offset), 0))
        for offset in range(days - 1, -1, -1)
    ]


def _not_configured() -> bool:
    return not settings.system_admin_username or not settings.system_admin_password_hash


@router.get("", response_class=HTMLResponse)
def login_form(request: Request):
    return templates.TemplateResponse(request, "system_admin/login.html", {"errors": []})


@router.post("", response_class=HTMLResponse)
def login(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    db: Session = Depends(get_db),
):
    if _not_configured():
        return templates.TemplateResponse(
            request,
            "system_admin/login.html",
            {
                "errors": [
                    "This deployment has no system administrator configured yet. Set "
                    "SYSTEM_ADMIN_USERNAME and SYSTEM_ADMIN_PASSWORD_HASH to enable it."
                ]
            },
            status_code=503,
        )

    ok = tokens_match(settings.system_admin_username, (username or "").strip()) and verify_password(
        password, settings.system_admin_password_hash
    )
    if not ok:
        logging_service.record_platform(
            db, "SYSTEM_ADMIN_LOGIN_FAILED", (username or "").strip(), level="WARNING", request=request
        )
        repeated_platform_event(
            db,
            "SYSTEM_ADMIN_LOGIN_FAILED",
            request=request,
            message="Repeated failed /administrator login attempts.",
        )
        return templates.TemplateResponse(
            request,
            "system_admin/login.html",
            {"errors": ["Username or password is incorrect."]},
            status_code=401,
        )

    logging_service.record_platform(db, "SYSTEM_ADMIN_LOGIN", "", request=request)
    response = RedirectResponse("/administrator/dashboard", status_code=303)
    set_system_admin_cookie(response)
    return response


@router.get("/logout")
def logout():
    response = RedirectResponse("/administrator", status_code=303)
    clear_system_admin_cookie(response)
    return response


@router.get("/dashboard", response_class=HTMLResponse, dependencies=[Depends(require_system_admin)])
def dashboard(request: Request, db: Session = Depends(get_db)):
    now = datetime.utcnow()
    since_30d = now - timedelta(days=_DAYS_BACK)

    # --- lecturers -----------------------------------------------------------
    lecturers = db.scalars(select(Lecturer).order_by(Lecturer.created_at.desc())).all()
    verified_count = sum(1 for l in lecturers if l.is_verified)
    signups_by_day = _day_buckets([l.created_at for l in lecturers if l.created_at >= since_30d])

    # --- courses ---------------------------------------------------------------
    courses = db.scalars(select(Course).order_by(Course.created_at.desc())).all()
    lecturer_by_id = {l.id: l for l in lecturers}
    ready_count = sum(1 for c in courses if c.database_ready)
    by_storage_mode = Counter(c.course_storage_mode for c in courses)

    # --- exams: each course's OWN database, bounded/tolerant per course --------
    exam_rows_by_course: dict[int, list] = {}
    exam_errors_by_course: dict[int, str] = {}
    student_count_by_course: dict[int, int] = {}
    total_exams = 0
    total_questions = 0
    open_exams = 0
    total_students = 0
    for c in courses:
        if not c.database_ready:
            continue
        try:
            rows = fetch_course_exam_summary(c)
        except Exception as exc:
            exam_errors_by_course[c.id] = f"{type(exc).__name__}: {str(exc)[:200]}"
            continue
        exam_rows_by_course[c.id] = rows
        total_exams += len(rows)
        total_questions += sum(r.question_count for r in rows)
        open_exams += sum(1 for r in rows if r.is_open)
        try:
            student_count = len(fetch_course_students(c))
        except Exception:
            student_count = 0
        student_count_by_course[c.id] = student_count
        total_students += student_count

    # --- site visits -------------------------------------------------------------
    visits_30d = db.scalars(select(PageVisit).where(PageVisit.at >= since_30d)).all()
    visits_by_day = _day_buckets([v.at for v in visits_30d])
    total_visits = db.scalar(select(func.count(PageVisit.id))) or 0
    since_24h = now - timedelta(hours=24)
    visits_24h = sum(1 for v in visits_30d if v.at >= since_24h)
    unique_ips_30d = len({v.ip for v in visits_30d if v.ip})
    top_paths = Counter(v.path for v in visits_30d).most_common(15)

    # --- recent platform activity ------------------------------------------------
    recent_activity = db.scalars(
        select(PlatformLog).order_by(PlatformLog.at.desc()).limit(60)
    ).all()

    return templates.TemplateResponse(
        request,
        "system_admin/dashboard.html",
        {
            "lecturers": lecturers,
            "verified_count": verified_count,
            "signups_by_day": signups_by_day,
            "courses": courses,
            "lecturer_by_id": lecturer_by_id,
            "ready_count": ready_count,
            "by_storage_mode": dict(by_storage_mode),
            "exam_rows_by_course": exam_rows_by_course,
            "exam_errors_by_course": exam_errors_by_course,
            "student_count_by_course": student_count_by_course,
            "total_exams": total_exams,
            "total_questions": total_questions,
            "open_exams": open_exams,
            "total_students": total_students,
            "total_visits": total_visits,
            "visits_24h": visits_24h,
            "visits_30d_count": len(visits_30d),
            "visits_by_day": visits_by_day,
            "unique_ips_30d": unique_ips_30d,
            "top_paths": top_paths,
            "recent_activity": recent_activity,
        },
    )
