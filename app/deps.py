"""Shared request dependencies: which course this request is for, who is signed in
within it, and the templating environment.

Every course-scoped route lives under /{slug}/..., and almost everything here exists
to turn that one path segment into (a) the right Lecturer row from the platform
database, (b) a Session bound to THAT lecturer's own database, and (c) session-cookie
checks that reject a cookie issued for a different slug - a bare numeric id in a
cookie means nothing on its own once there are many lecturers' databases in play.
"""

import os
from dataclasses import dataclass

from fastapi import Depends, HTTPException, Request, status
from fastapi.templating import Jinja2Templates
from jinja2 import pass_context
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.db import get_db
from app.models_course import Student
from app.models_platform import Lecturer
from app.security import read_session_cookie
from app.tenant_db import course_session

templates = Jinja2Templates(
    directory=os.path.join(os.path.dirname(os.path.abspath(__file__)), "templates")
)

# Deliberately NOT a contextvars.ContextVar here. FastAPI dispatches each sync
# dependency via anyio's run_in_threadpool, and every one of those calls runs
# inside its own COPY of the current context - a var.set() performed inside
# get_lecturer's copy never propagates back to the route handler's context, so
# a contextvar silently reads back its default everywhere it's actually used.
# request.state is a plain attribute on the one shared Request object instead,
# which every dependency and the route handler all genuinely share. Jinja's
# pass_context then pulls that same `request` back out of the render context
# (Jinja2Templates always injects it) without every template call site having
# to pass it explicitly.


def _lecturer_from(context) -> Lecturer | None:
    request = context.get("request")
    return getattr(request.state, "lecturer", None) if request is not None else None


@pass_context
def course_url(context, path: str = "") -> str:
    """Jinja global: prefixes a path with the current request's course slug.
    Outside any course context (platform-level pages) returns the path unchanged -
    those pages never call this on anything but harmless self-links."""
    lecturer = _lecturer_from(context)
    if lecturer is None:
        return path or "/"
    return f"/{lecturer.slug}{path}"


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
    platform-level pages, where there is no lecturer at all - everything falls
    back to the platform's own generic name."""
    lecturer = _lecturer_from(context)
    return _CourseBranding(
        brand=(lecturer.brand if lecturer else "") or settings.app_name,
        subtitle=(lecturer.subtitle if lecturer else "") or "Assessment with clarity and integrity.",
        footer=(lecturer.footer if lecturer else "") or settings.app_name,
        slug=lecturer.slug if lecturer else "",
    )


templates.env.globals["course_url"] = course_url
templates.env.globals["course"] = course
templates.env.globals["app_name"] = settings.app_name
templates.env.globals["base_url"] = settings.base_url.rstrip("/")
# Deliberately not the whole `settings` object - it holds secrets (secret_key,
# smtp_password, resend_api_key). Only specific, safe values get exposed.
templates.env.globals["verification_token_hours"] = settings.verification_token_max_age_seconds // 3600


# ------------------------------------------------------------------------ tenancy


def get_lecturer(request: Request, slug: str, platform_db: Session = Depends(get_db)) -> Lecturer:
    """Resolves the URL's /{slug} to a Lecturer row and marks this request as
    belonging to that course for course_url()/course - see the note above on
    why that's request.state and not a contextvar. Reachable even before the
    lecturer's own database is connected - the setup page needs that."""
    lecturer = platform_db.scalar(select(Lecturer).where(Lecturer.slug == slug))
    if lecturer is None:
        raise HTTPException(status_code=404, detail="No course found at this address.")
    request.state.lecturer = lecturer
    return lecturer


def require_course_ready(lecturer: Lecturer = Depends(get_lecturer)) -> Lecturer:
    """Gate for every route that actually touches course data. A lecturer who
    hasn't connected (and had validated) a database yet gets sent to finish
    that, instead of every student-facing route failing deep in tenant_db."""
    if not lecturer.database_ready:
        raise HTTPException(
            status_code=status.HTTP_303_SEE_OTHER,
            headers={"Location": f"/{lecturer.slug}/admin/setup"},
            detail="This course has not finished being set up.",
        )
    return lecturer


def get_course_db(lecturer: Lecturer = Depends(require_course_ready)):
    yield from course_session(lecturer)


# --------------------------------------------------------------------------- auth


def session_data(request: Request) -> dict | None:
    return read_session_cookie(request.cookies.get(settings.session_cookie))


def current_student(
    request: Request,
    lecturer: Lecturer = Depends(require_course_ready),
    db: Session = Depends(get_course_db),
) -> Student | None:
    data = session_data(request)
    if not data or data.get("role") != "student" or data.get("slug") != lecturer.slug:
        return None
    student = db.get(Student, data.get("id"))
    if student is None or student.is_blocked:
        return None
    return student


def require_student(
    request: Request,
    lecturer: Lecturer = Depends(require_course_ready),
    db: Session = Depends(get_course_db),
) -> Student:
    student = current_student(request, lecturer, db)
    if student is None:
        raise HTTPException(
            status_code=status.HTTP_303_SEE_OTHER,
            headers={"Location": f"/{lecturer.slug}/login"},
            detail="Sign in required.",
        )
    if not student.can_sit:
        raise HTTPException(
            status_code=status.HTTP_303_SEE_OTHER,
            headers={"Location": f"/{lecturer.slug}/login?pending=1"},
            detail="Account not ready.",
        )
    return student


def current_admin(
    request: Request,
    lecturer: Lecturer = Depends(get_lecturer),
    platform_db: Session = Depends(get_db),
) -> Lecturer | None:
    """The signed-in course owner - looked up in the PLATFORM database (that's
    where Lecturer accounts live), not the course database current_student uses."""
    data = session_data(request)
    if not data or data.get("role") != "admin" or data.get("slug") != lecturer.slug:
        return None
    found = platform_db.get(Lecturer, data.get("id"))
    if found is None or found.slug != lecturer.slug:
        return None
    return found


def require_admin(
    request: Request,
    lecturer: Lecturer = Depends(get_lecturer),
    platform_db: Session = Depends(get_db),
) -> Lecturer:
    """Auth only - does NOT require the course database to be ready, so the
    setup page itself stays reachable. Routes that touch course data should
    depend on require_admin_ready instead."""
    admin = current_admin(request, lecturer, platform_db)
    if admin is None:
        raise HTTPException(
            status_code=status.HTTP_303_SEE_OTHER,
            headers={"Location": f"/{lecturer.slug}/admin/login"},
            detail="Admin sign in required.",
        )
    return admin


def require_admin_ready(
    admin: Lecturer = Depends(require_admin),
    lecturer: Lecturer = Depends(require_course_ready),
) -> Lecturer:
    """Auth AND a connected database - what every admin route except /setup wants."""
    return admin


def client_ip(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for", "")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else ""
