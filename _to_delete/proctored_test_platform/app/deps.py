"""Shared request dependencies: who is signed in, and the templating environment."""

from fastapi import Depends, HTTPException, Request, status
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

import os

from app.config import settings
from app.db import get_db
from app.models import Admin, Student
from app.security import read_session_cookie

templates = Jinja2Templates(
    directory=os.path.join(os.path.dirname(os.path.abspath(__file__)), "templates")
)
templates.env.globals["settings"] = settings


def session_data(request: Request) -> dict | None:
    return read_session_cookie(request.cookies.get(settings.session_cookie))


def current_student(request: Request, db: Session = Depends(get_db)) -> Student | None:
    data = session_data(request)
    if not data or data.get("role") != "student":
        return None
    student = db.get(Student, data.get("id"))
    if student is None or student.is_blocked:
        return None
    return student


def require_student(
    request: Request, db: Session = Depends(get_db)
) -> Student:
    student = current_student(request, db)
    if student is None:
        raise HTTPException(
            status_code=status.HTTP_303_SEE_OTHER,
            headers={"Location": "/login"},
            detail="Sign in required.",
        )
    if not student.can_sit:
        raise HTTPException(
            status_code=status.HTTP_303_SEE_OTHER,
            headers={"Location": "/login?pending=1"},
            detail="Account not ready.",
        )
    return student


def current_admin(request: Request, db: Session = Depends(get_db)) -> Admin | None:
    data = session_data(request)
    if not data or data.get("role") != "admin":
        return None
    return db.get(Admin, data.get("id"))


def require_admin(request: Request, db: Session = Depends(get_db)) -> Admin:
    admin = current_admin(request, db)
    if admin is None:
        raise HTTPException(
            status_code=status.HTTP_303_SEE_OTHER,
            headers={"Location": "/admin/login"},
            detail="Admin sign in required.",
        )
    return admin


def client_ip(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for", "")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else ""
