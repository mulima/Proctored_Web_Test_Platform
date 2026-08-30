"""Shared request dependencies: which course this request is for, who is signed in
within it, and the templating environment.

Every course-scoped route lives under /{slug}/..., and almost everything here exists
to turn that one path segment into (a) the right Course row, (b) a Session bound to
THAT course's own database, and (c) session-cookie checks appropriate to who's
signing in - a student's session is still tied to one specific course (see
set_session_cookie in app/security.py); a lecturer's session is tied to their
*account*, which may own several courses, so admin auth is a real ownership check
(Course.lecturer_id == account.id) rather than a slug match.
"""

import os
from dataclasses import dataclass

from fastapi import Depends, HTTPException, Request, status
from fastapi.templating import Jinja2Templates
from jinja2 import pass_context
from sqlalchemy.exc import InterfaceError, OperationalError
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.db import get_db
from app.models_course import Student
from app import logging_service
from app.models_platform import Course, Lecturer
from app.monitoring import repeated_platform_event
from app.security import read_session_cookie
from app.tenant_db import course_session

templates = Jinja2Templates(
    directory=os.path.join(os.path.dirname(os.path.abspath(__file__)), "templates")
)

# Deliberately NOT a contextvars.ContextVar here. FastAPI dispatches each sync
# dependency via anyio's run_in_threadpool, and every one of those calls runs
# inside its own COPY of the current context - a var.set() performed inside
# get_course's copy never propagates back to the route handler's context, so
# a contextvar silently reads back its default everywhere it's actually used.
# request.state is a plain attribute on the one shared Request object instead,
# which every dependency and the route handler all genuinely share. Jinja's
# pass_context then pulls that same `request` back out of the render context
# (Jinja2Templates always injects it) without every template call site having
# to pass it explicitly.


@pass_context
def course_url(context, path: str = "") -> str:
    """Jinja global: prefixes a path with the current request's course slug.
    Outside any course context (platform-level pages) returns the path unchanged -
    those pages never call this on anything but harmless self-links."""
    request = context.get("request")
    slug = getattr(request.state, "course_slug", "") if request is not None else ""
    if not slug:
        return path or "/"
    return f"/{slug}{path}"


@dataclass
class _CourseBranding:
    brand: str
    subtitle: str
    footer: str
    slug: str


@pass_context
def course(context) -> _CourseBranding:
    """Jinja global: `{{ course().brand }}` etc - the current request's course
    identity, with the same generic fallbacks Settings.brand/subtitle/footer used
    to provide when a course had nothing configured. Works identically on
    platform-level pages, where there is no course at all - everything falls
    back to the platform's own generic name."""
    request = context.get("request")
    brand = getattr(request.state, "course_brand", "") if request is not None else ""
    subtitle = (
        getattr(request.state, "course_subtitle", "") if request is not None else ""
    )
    footer = getattr(request.state, "course_footer", "") if request is not None else ""
    slug = getattr(request.state, "course_slug", "") if request is not None else ""
    return _CourseBranding(
        brand=brand or settings.app_name,
        subtitle=subtitle or "Assessment with clarity and integrity.",
        footer=footer or settings.app_name,
        slug=slug,
    )


templates.env.globals["course_url"] = course_url
templates.env.globals["course"] = course
templates.env.globals["app_name"] = settings.app_name
templates.env.globals["base_url"] = settings.base_url.rstrip("/")
# Deliberately not the whole `settings` object - it holds secrets (secret_key,
# smtp_password, resend_api_key). Only specific, safe values get exposed.
templates.env.globals["verification_token_hours"] = settings.verification_token_max_age_seconds // 3600


# ------------------------------------------------------------------------ tenancy


def get_course(request: Request, slug: str, platform_db: Session = Depends(get_db)) -> Course:
    """Resolves the URL's /{slug} to a Course row and marks this request as
    belonging to that course for course_url()/course - see the note above on
    why that's request.state and not a contextvar. Reachable even before the
    course's own database is connected - the setup page needs that."""
    course_row = platform_db.scalar(select(Course).where(Course.slug == slug))
    if course_row is None:
        raise HTTPException(status_code=404, detail="No course found at this address.")
    request.state.course = course_row
    request.state.course_slug = course_row.slug
    request.state.course_brand = course_row.course_code or settings.app_name
    request.state.course_subtitle = course_row.course_title or "Assessment with clarity and integrity."
    request.state.course_footer = course_row.institution or settings.app_name
    return course_row


def require_course_ready(course: Course = Depends(get_course)) -> Course:
    """Gate for every route that actually touches course data. A course that
    hasn't been connected (and had validated) a database yet sends the visitor
    to finish that, instead of every student-facing route failing deep in
    tenant_db."""
    if not course.database_ready:
        raise HTTPException(
            status_code=status.HTTP_303_SEE_OTHER,
            headers={"Location": f"/{course.slug}/admin/setup"},
            detail="This course has not finished being set up.",
        )
    return course


def get_course_db(course: Course = Depends(require_course_ready)):
    try:
        yield from course_session(course)
    except HTTPException:
        raise
    except (OperationalError, InterfaceError):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=(
                "Course database is unavailable for this course. "
                f"Reconnect it at /{course.slug}/admin/setup."
            ),
        )


# --------------------------------------------------------------------------- auth


def session_data(request: Request) -> dict | None:
    return read_session_cookie(request.cookies.get(settings.session_cookie))


def current_student(
    request: Request,
    course: Course = Depends(require_course_ready),
    db: Session = Depends(get_course_db),
    platform_db: Session = Depends(get_db),
) -> Student | None:
    data = session_data(request)
    if not data or data.get("role") != "student":
        return None
    if data.get("slug") != course.slug:
        logging_service.record_platform(
            platform_db,
            "CROSS_COURSE_ACCESS",
            "Session cookie course does not match request course.",
            request=request,
            payload={"requested_slug": course.slug},
        )
        repeated_platform_event(
            platform_db,
            "CROSS_COURSE_ACCESS",
            request=request,
            message="Repeated cross-course session access patterns detected.",
        )
        return None
    student = db.get(Student, data.get("id"))
    if student is None or student.is_blocked:
        return None
    return student


def require_student(
    request: Request,
    course: Course = Depends(require_course_ready),
    db: Session = Depends(get_course_db),
) -> Student:
    student = current_student(request, course, db)
    if student is None:
        raise HTTPException(
            status_code=status.HTTP_303_SEE_OTHER,
            headers={"Location": f"/{course.slug}/login"},
            detail="Sign in required.",
        )
    if not student.can_sit:
        raise HTTPException(
            status_code=status.HTTP_303_SEE_OTHER,
            headers={"Location": f"/{course.slug}/login?pending=1"},
            detail="Account not ready.",
        )
    return student


def current_account(request: Request, platform_db: Session = Depends(get_db)) -> Lecturer | None:
    """The signed-in lecturer *account*, independent of any one course - used by
    platform-level routes (/admin/courses, /login) that aren't scoped to a slug."""
    data = session_data(request)
    if not data or data.get("role") != "admin":
        return None
    return platform_db.get(Lecturer, data.get("id"))


def require_account(request: Request, platform_db: Session = Depends(get_db)) -> Lecturer:
    account = current_account(request, platform_db)
    if account is None:
        raise HTTPException(
            status_code=status.HTTP_303_SEE_OTHER,
            headers={"Location": "/login"},
            detail="Sign in required.",
        )
    return account


def current_admin(
    request: Request,
    course: Course = Depends(get_course),
    platform_db: Session = Depends(get_db),
) -> Lecturer | None:
    """The signed-in course owner - looked up in the PLATFORM database (that's
    where Lecturer accounts live), not the course database current_student uses.
    The account isn't slug-scoped (it may own several courses); what's checked
    here is real ownership of *this* course, not a match against a slug stashed
    in the cookie."""
    account = current_account(request, platform_db)
    if account is None:
        return None
    if course.lecturer_id != account.id:
        return None
    return account


def require_admin(
    request: Request,
    course: Course = Depends(get_course),
    platform_db: Session = Depends(get_db),
) -> Lecturer:
    """Auth + ownership only - does NOT require the course database to be ready, so
    the setup page itself stays reachable. Routes that touch course data should
    depend on require_admin_ready instead."""
    admin = current_admin(request, course, platform_db)
    if admin is None:
        raise HTTPException(
            status_code=status.HTTP_303_SEE_OTHER,
            headers={"Location": f"/{course.slug}/admin/login"},
            detail="Admin sign in required.",
        )
    return admin


def require_admin_ready(
    admin: Lecturer = Depends(require_admin),
    course: Course = Depends(require_course_ready),
) -> Course:
    """Auth, ownership AND a connected database - what every admin route except
    /setup wants. Returns the Course (what nearly every admin route actually
    needs); depend on require_admin too if you also need the account itself."""
    return course


def client_ip(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for", "")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else ""
