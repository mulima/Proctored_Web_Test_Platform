"""Application entry point.

On start the app makes sure the admin account matches ADMIN_PASSWORD, and nothing else.
It never creates or alters tables: the schema belongs to Alembic, so a redeploy cannot
quietly reshape a database that has real submissions in it.
"""

import os
import sys
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import func, inspect, select
from starlette.exceptions import HTTPException as StarletteHTTPException

from app import logging_service
from app.config import settings
from app.db import SessionLocal, engine
from app.deps import templates
from app.models import Admin
from app.routers import admin as admin_router
from app.routers import auth as auth_router
from app.routers import exam as exam_router
from app.security import hash_password

APP_DIR = os.path.dirname(os.path.abspath(__file__))


def ensure_admin() -> None:
    """Create or reset the admin account from the environment, at every start."""
    if not settings.admin_password:
        print(
            "[startup] ADMIN_PASSWORD is not set. The admin panel is unreachable until "
            "it is. Set it as an environment variable and restart.",
            flush=True,
        )
        return

    inspector = inspect(engine)
    if not inspector.has_table("admins"):
        print(
            "[startup] Tables are missing. Run 'alembic upgrade head' before starting.",
            flush=True,
        )
        return

    with SessionLocal() as db:
        email = settings.admin_email.strip().lower()
        admin = db.scalar(select(Admin).where(func.lower(Admin.email) == email))
        if admin is None:
            admin = Admin(email=email, password_hash=hash_password(settings.admin_password))
            db.add(admin)
            db.commit()
            logging_service.record(db, "ADMIN_CREATED", email, level="WARNING")
            print(f"[startup] Admin account created for {email}.", flush=True)
            return

        from app.security import verify_password

        if not verify_password(settings.admin_password, admin.password_hash):
            admin.password_hash = hash_password(settings.admin_password)
            admin.password_set_at = func.now()
            db.commit()
            logging_service.record(db, "ADMIN_PASSWORD_RESET", email, level="WARNING")
            print(f"[startup] Admin password updated for {email}.", flush=True)


def check_schema() -> None:
    inspector = inspect(engine)
    missing = [
        name
        for name in ("students", "exams", "questions", "attempts", "answers", "incidents", "app_logs")
        if not inspector.has_table(name)
    ]
    if missing:
        print(
            "[startup] Missing tables: " + ", ".join(missing) + ". Run 'alembic upgrade head'.",
            flush=True,
        )


@asynccontextmanager
async def lifespan(app: FastAPI):
    check_schema()
    ensure_admin()
    if settings.mail_backend == "console":
        print(
            "[startup] MAIL_BACKEND=console: emails are printed here, not delivered.",
            flush=True,
        )
    yield


app = FastAPI(title=settings.app_name, lifespan=lifespan, docs_url=None, redoc_url=None)
app.mount("/static", StaticFiles(directory=os.path.join(APP_DIR, "static")), name="static")

app.include_router(auth_router.router)
app.include_router(exam_router.router)
app.include_router(admin_router.router)


@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "same-origin")
    return response


@app.exception_handler(StarletteHTTPException)
async def http_exception(request: Request, exc: StarletteHTTPException):
    # The auth dependencies signal "not signed in" by raising a 303 with a Location.
    # Turn that into a real redirect rather than a JSON body carrying a header.
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
    """Railway health check. Touches the database so a broken connection shows up."""
    try:
        with SessionLocal() as db:
            db.execute(select(1))
        return {"ok": True}
    except Exception as exc:
        return {"ok": False, "error": str(exc)[:200]}


@app.get("/privacy", response_class=HTMLResponse)
def privacy(request: Request):
    return templates.TemplateResponse(request, "privacy.html", {})


def main() -> None:
    import uvicorn

    port = int(os.environ.get("PORT", "8000"))
    uvicorn.run("app.main:app", host="0.0.0.0", port=port, log_level="info")


if __name__ == "__main__":
    sys.exit(main())
