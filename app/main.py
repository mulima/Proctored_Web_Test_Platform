"""Application entry point.

This process is a shared platform: any lecturer can sign up at /signup, and every
course-specific route lives under /{slug}/... - see app/deps.py for how a request's
slug becomes the right Lecturer and the right database. Nothing here bootstraps a
single admin account any more; signing up replaces that entirely.
"""

import os
import re
import sys
from contextlib import asynccontextmanager
from datetime import datetime

from fastapi import APIRouter, Depends, FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import func, inspect, select
from sqlalchemy.orm import Session
from starlette.exceptions import HTTPException as StarletteHTTPException

from app import logging_service
from app.config import settings
from app.db import SessionLocal, engine, get_db
from app.deps import templates
from app.mailer import Message, send
from app.models_platform import Lecturer
from app.routers import admin as admin_router
from app.routers import auth as auth_router
from app.routers import exam as exam_router
from app.security import (
    hash_password,
    make_lecturer_verification_token,
    password_problems,
    read_lecturer_verification_token,
    tokens_match,
)

APP_DIR = os.path.dirname(os.path.abspath(__file__))

RESERVED_SLUGS = {
    "admin", "static", "healthz", "privacy", "signup", "login", "logout",
    "register", "verify", "resend", "verify-lecturer", "resend-lecturer",
    "api", "docs", "redoc", "openapi.json",
}


def _send_lecturer_verification_email(db: Session, lecturer: Lecturer, request: Request) -> bool:
    token = make_lecturer_verification_token(lecturer.email)
    lecturer.verification_token = token
    lecturer.verification_sent_at = datetime.utcnow()
    db.commit()

    link = f"{settings.base_url.rstrip('/')}/verify-lecturer?token={token}"
    body = (
        f"Hello,\n\n"
        f"A lecturer account was registered on {settings.app_name} for course address "
        f"/{lecturer.slug}, using this email address.\n\n"
        "Confirm the address by opening this link:\n\n"
        f"{link}\n\n"
        f"The link works once and expires in "
        f"{settings.verification_token_max_age_seconds // 3600} hours.\n\n"
        "If you did not request this, ignore this message and the account stays unusable.\n\n"
        f"{settings.app_name}\n"
    )
    delivered = send(
        Message(to=lecturer.email, subject=f"Confirm your {settings.app_name} account", body=body)
    )
    logging_service.record_platform(
        db,
        "LECTURER_VERIFICATION_EMAIL_SENT" if delivered else "LECTURER_VERIFICATION_EMAIL_FAILED",
        f"Verification link to {lecturer.email}",
        level="INFO" if delivered else "ERROR",
        lecturer_id=lecturer.id,
        request=request,
    )
    return delivered


def check_schema() -> None:
    inspector = inspect(engine)
    missing = [name for name in ("lecturers", "platform_logs") if not inspector.has_table(name)]
    if missing:
        print(
            "[startup] Missing tables: " + ", ".join(missing) + ". Run 'alembic upgrade head'.",
            flush=True,
        )


@asynccontextmanager
async def lifespan(app: FastAPI):
    check_schema()
    if settings.mail_backend == "console":
        print(
            "[startup] MAIL_BACKEND=console: emails are printed here, not delivered.",
            flush=True,
        )
    if not settings.credential_encryption_key:
        print(
            "[startup] CREDENTIAL_ENCRYPTION_KEY is not set. Lecturers will not be able "
            "to connect a database until it is.",
            flush=True,
        )
    yield


app = FastAPI(title=settings.app_name, lifespan=lifespan, docs_url=None, redoc_url=None)
app.mount("/static", StaticFiles(directory=os.path.join(APP_DIR, "static")), name="static")

_DOCS_DIR = os.path.join(os.path.dirname(APP_DIR), "docs")
if os.path.isdir(_DOCS_DIR):
    # docs/DATABASE_SCHEMA.md and .sql need to be a real, linkable URL - a lecturer
    # setting up their own database is pointed here from /signup and /{slug}/admin/setup.
    app.mount("/docs", StaticFiles(directory=_DOCS_DIR), name="docs")


@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "same-origin")
    return response


@app.exception_handler(StarletteHTTPException)
async def http_exception(request: Request, exc: StarletteHTTPException):
    # The auth dependencies signal "not signed in" / "not set up yet" by raising a
    # 303 with a Location. Turn that into a real redirect rather than a JSON body
    # carrying a header.
    location = (exc.headers or {}).get("Location")
    if exc.status_code in (302, 303, 307) and location:
        return RedirectResponse(location, status_code=303)
    if exc.status_code == 404:
        return templates.TemplateResponse(request, "404.html", {}, status_code=404)
    return templates.TemplateResponse(
        request,
        "error.html",
        {"status_code": exc.status_code, "detail": exc.detail},
        status_code=exc.status_code,
    )


@app.get("/healthz")
def healthz():
    """Railway health check. Touches the platform database so a broken
    connection shows up - it says nothing about any individual lecturer's
    own database, which this process only reaches per-request."""
    try:
        with SessionLocal() as db:
            db.execute(select(1))
        return {"ok": True}
    except Exception as exc:
        return {"ok": False, "error": str(exc)[:200]}


@app.get("/privacy", response_class=HTMLResponse)
def privacy(request: Request):
    return templates.TemplateResponse(request, "privacy.html", {})


@app.get("/data-collection", response_class=HTMLResponse)
def data_collection(request: Request):
    return templates.TemplateResponse(request, "data_collection.html", {})


@app.get("/data-retention", response_class=HTMLResponse)
def data_retention(request: Request):
    return templates.TemplateResponse(request, "data_retention.html", {})


# --------------------------------------------------------------------- platform-level


@app.get("/", response_class=HTMLResponse)
def landing(request: Request):
    return templates.TemplateResponse(request, "landing.html", {})


_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,58}[a-z0-9]$")


@app.get("/signup", response_class=HTMLResponse)
def signup_form(request: Request):
    return templates.TemplateResponse(request, "signup.html", {"errors": [], "values": {}})


@app.post("/signup", response_class=HTMLResponse)
def signup(
    request: Request,
    email: str = Form(...),
    password: str = Form(...),
    confirm_password: str = Form(...),
    slug: str = Form(...),
    course_code: str = Form(""),
    course_title: str = Form(""),
    institution: str = Form(""),
    db: Session = Depends(get_db),
):
    email = (email or "").strip().lower()
    slug = (slug or "").strip().lower()
    values = {
        "email": email, "slug": slug, "course_code": course_code,
        "course_title": course_title, "institution": institution,
    }
    errors: list[str] = []

    if "@" not in email or "." not in email.rsplit("@", 1)[-1]:
        errors.append("Enter a valid email address.")
    if not _SLUG_RE.match(slug):
        errors.append(
            "Course address must be 3-60 characters: lowercase letters, numbers and "
            "hyphens, not starting or ending with a hyphen."
        )
    elif slug in RESERVED_SLUGS:
        errors.append("That course address is reserved. Choose another.")
    if password != confirm_password:
        errors.append("The two passwords do not match.")
    errors.extend(password_problems(password, course_code=course_code))

    if not errors:
        if db.scalar(select(Lecturer).where(func.lower(Lecturer.email) == email)):
            errors.append("An account with this email already exists.")
        if db.scalar(select(Lecturer).where(Lecturer.slug == slug)):
            errors.append("That course address is already taken.")

    if errors:
        return templates.TemplateResponse(
            request, "signup.html", {"errors": errors, "values": values}, status_code=400
        )

    lecturer = Lecturer(
        email=email,
        password_hash=hash_password(password),
        slug=slug,
        course_code=course_code.strip(),
        course_title=course_title.strip(),
        institution=institution.strip(),
        database_ready=False,
        is_verified=False,
        password_set_at=datetime.utcnow(),
    )
    db.add(lecturer)
    db.commit()
    db.refresh(lecturer)
    logging_service.record_platform(
        db, "LECTURER_SIGNED_UP", f"{email} at /{slug}", lecturer_id=lecturer.id, request=request
    )
    _send_lecturer_verification_email(db, lecturer, request)

    return templates.TemplateResponse(request, "signup_done.html", {"email": email, "slug": slug})


@app.get("/verify-lecturer", response_class=HTMLResponse)
def verify_lecturer(request: Request, token: str = "", db: Session = Depends(get_db)):
    email = read_lecturer_verification_token(token)
    lecturer = (
        db.scalar(select(Lecturer).where(func.lower(Lecturer.email) == email)) if email else None
    )

    if lecturer is None:
        return templates.TemplateResponse(
            request,
            "verify_lecturer.html",
            {"ok": False, "message": "That verification link is not valid.", "slug": ""},
            status_code=400,
        )
    if lecturer.is_verified:
        return templates.TemplateResponse(
            request,
            "verify_lecturer.html",
            {"ok": True, "message": "This address is already confirmed. You can sign in.", "slug": lecturer.slug},
        )
    if not tokens_match(lecturer.verification_token, token):
        return templates.TemplateResponse(
            request,
            "verify_lecturer.html",
            {
                "ok": False,
                "message": "That link has already been used or has been replaced by a newer one.",
                "slug": "",
            },
            status_code=400,
        )

    lecturer.is_verified = True
    lecturer.verified_at = datetime.utcnow()
    lecturer.verification_token = None
    db.commit()
    logging_service.record_platform(
        db, "LECTURER_EMAIL_VERIFIED", lecturer.email, lecturer_id=lecturer.id, request=request
    )
    return templates.TemplateResponse(
        request,
        "verify_lecturer.html",
        {
            "ok": True,
            "message": "Your email address is confirmed. You can sign in and set up your course.",
            "slug": lecturer.slug,
        },
    )


@app.get("/resend-lecturer", response_class=HTMLResponse)
def resend_lecturer_form(request: Request):
    return templates.TemplateResponse(request, "resend_lecturer.html", {"sent": False})


@app.post("/resend-lecturer", response_class=HTMLResponse)
def resend_lecturer(request: Request, email: str = Form(...), db: Session = Depends(get_db)):
    email = (email or "").strip().lower()
    lecturer = db.scalar(select(Lecturer).where(func.lower(Lecturer.email) == email))
    if lecturer and not lecturer.is_verified:
        _send_lecturer_verification_email(db, lecturer, request)
    # Always the same answer, so this cannot be used to enumerate who has signed up.
    return templates.TemplateResponse(request, "resend_lecturer.html", {"sent": True})


# ------------------------------------------------------------------------- course-scoped

course_router = APIRouter(prefix="/{slug}")
course_router.include_router(auth_router.router)
course_router.include_router(exam_router.router)
course_router.include_router(admin_router.router)  # already prefixed "/admin" internally
app.include_router(course_router)


def main() -> None:
    import uvicorn

    port = int(os.environ.get("PORT", "8000"))
    uvicorn.run("app.main:app", host="0.0.0.0", port=port, log_level="info")


if __name__ == "__main__":
    sys.exit(main())
