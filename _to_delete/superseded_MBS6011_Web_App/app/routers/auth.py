"""Student registration, email verification and sign-in.

Registration is open ahead of the sitting and the emailed link is the gate: an account
cannot sit a test until the address it was registered with has been proved. Set
REQUIRE_ADMIN_APPROVAL=true to add a second gate and turn the roster into an allowlist
you approve by hand.
"""

from datetime import datetime

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app import logging_service
from app.config import settings
from app.db import get_db
from app.deps import templates
from app.mailer import Message, send
from app.models import Student
from app.security import (
    hash_password,
    make_session_cookie,
    make_verification_token,
    password_problems,
    read_verification_token,
    tokens_match,
    verify_password,
)

router = APIRouter()


def _normalise_email(email: str) -> str:
    return (email or "").strip().lower()


def _normalise_computer_number(value: str) -> str:
    # Computer numbers get typed with stray spaces and inconsistent case constantly.
    return "".join((value or "").split()).upper()


def _domain_allowed(email: str) -> bool:
    allowed = settings.allowed_domains
    if not allowed:
        return True
    domain = email.rsplit("@", 1)[-1].lower()
    return any(domain == d or domain.endswith("." + d) for d in allowed)


def _set_session(response: RedirectResponse, payload: dict) -> RedirectResponse:
    response.set_cookie(
        settings.session_cookie,
        make_session_cookie(payload),
        max_age=settings.session_max_age_seconds,
        httponly=True,
        samesite="lax",
        secure=settings.base_url.startswith("https"),
    )
    return response


def send_verification_email(db: Session, student: Student, request: Request) -> bool:
    token = make_verification_token(student.email)
    student.verification_token = token
    student.verification_sent_at = datetime.utcnow()
    db.commit()

    link = f"{settings.base_url.rstrip('/')}/verify?token={token}"
    body = (
        f"Hello {student.full_name},\n\n"
        f"An account was registered for {settings.course_title} using this email address.\n\n"
        f"Computer number: {student.computer_number}\n\n"
        "Confirm the address by opening this link:\n\n"
        f"{link}\n\n"
        f"The link works once and expires in "
        f"{settings.verification_token_max_age_seconds // 3600} hours.\n\n"
        "If you did not register, ignore this message and the account stays unusable.\n\n"
        f"{settings.institution}\n"
    )
    delivered = send(
        Message(to=student.email, subject=f"Confirm your {settings.course_code} account", body=body)
    )
    logging_service.record(
        db,
        "VERIFICATION_EMAIL_SENT" if delivered else "VERIFICATION_EMAIL_FAILED",
        f"Verification link to {student.email}",
        level="INFO" if delivered else "ERROR",
        student_id=student.id,
        request=request,
    )
    return delivered


@router.get("/register", response_class=HTMLResponse)
def register_form(request: Request):
    return templates.TemplateResponse(request, "register.html", {"errors": [], "values": {}})


@router.post("/register", response_class=HTMLResponse)
def register(
    request: Request,
    full_name: str = Form(...),
    email: str = Form(...),
    computer_number: str = Form(...),
    password: str = Form(...),
    confirm_password: str = Form(...),
    db: Session = Depends(get_db),
):
    email = _normalise_email(email)
    computer_number = _normalise_computer_number(computer_number)
    full_name = (full_name or "").strip()
    values = {"full_name": full_name, "email": email, "computer_number": computer_number}
    errors: list[str] = []

    if len(full_name) < 3:
        errors.append("Enter your full name as it appears on the register.")
    if "@" not in email or "." not in email.rsplit("@", 1)[-1]:
        errors.append("Enter a valid email address.")
    elif not _domain_allowed(email):
        errors.append(
            "Register with your university email address ("
            + ", ".join(settings.allowed_domains)
            + ")."
        )
    if len(computer_number) < 4:
        errors.append("Enter your computer number.")
    if password != confirm_password:
        errors.append("The two passwords do not match.")
    errors.extend(password_problems(password))

    if not errors:
        existing_email = db.scalar(select(Student).where(func.lower(Student.email) == email))
        existing_number = db.scalar(
            select(Student).where(Student.computer_number == computer_number)
        )
        if existing_email:
            if existing_email.is_verified:
                errors.append("An account with this email already exists. Sign in instead.")
            else:
                # Re-registering an unverified address just resends the link. Friendlier
                # than an error, and it cannot be used to discover verified accounts.
                send_verification_email(db, existing_email, request)
                return templates.TemplateResponse(
                    request, "register_done.html", {"email": email, "resent": True}
                )
        if existing_number and existing_number.email != email:
            errors.append("That computer number is already registered to another account.")

    if errors:
        logging_service.record(
            db,
            "REGISTRATION_REJECTED",
            "; ".join(errors),
            level="WARNING",
            request=request,
            payload=values,
        )
        return templates.TemplateResponse(
            request, "register.html", {"errors": errors, "values": values}, status_code=400
        )

    student = Student(
        full_name=full_name,
        email=email,
        computer_number=computer_number,
        password_hash=hash_password(password),
        is_verified=False,
        is_approved=not settings.require_admin_approval,
    )
    db.add(student)
    db.commit()
    db.refresh(student)

    logging_service.record(
        db,
        "REGISTERED",
        f"{full_name} <{email}> computer number {computer_number}",
        student_id=student.id,
        request=request,
    )
    send_verification_email(db, student, request)
    return templates.TemplateResponse(
        request, "register_done.html", {"email": email, "resent": False}
    )


@router.get("/verify", response_class=HTMLResponse)
def verify(request: Request, token: str = "", db: Session = Depends(get_db)):
    email = read_verification_token(token)
    student = (
        db.scalar(select(Student).where(func.lower(Student.email) == email)) if email else None
    )

    if student is None:
        return templates.TemplateResponse(
            request,
            "verify.html",
            {"ok": False, "message": "That verification link is not valid."},
            status_code=400,
        )
    if student.is_verified:
        return templates.TemplateResponse(
            request,
            "verify.html",
            {"ok": True, "message": "This address is already confirmed. You can sign in."},
        )
    if not tokens_match(student.verification_token, token):
        # The stored token is cleared on first use, so a replayed link lands here.
        return templates.TemplateResponse(
            request,
            "verify.html",
            {
                "ok": False,
                "message": "That link has already been used or has been replaced by a newer one.",
            },
            status_code=400,
        )

    student.is_verified = True
    student.verified_at = datetime.utcnow()
    student.verification_token = None
    db.commit()
    logging_service.record(
        db, "EMAIL_VERIFIED", student.email, student_id=student.id, request=request
    )

    message = "Your email address is confirmed. You can sign in and sit the test when it opens."
    if not student.is_approved:
        message = (
            "Your email address is confirmed. An administrator still needs to approve your "
            "account before you can sit the test."
        )
    return templates.TemplateResponse(request, "verify.html", {"ok": True, "message": message})


@router.get("/resend", response_class=HTMLResponse)
def resend_form(request: Request):
    return templates.TemplateResponse(request, "resend.html", {"sent": False})


@router.post("/resend", response_class=HTMLResponse)
def resend(request: Request, email: str = Form(...), db: Session = Depends(get_db)):
    email = _normalise_email(email)
    student = db.scalar(select(Student).where(func.lower(Student.email) == email))
    if student and not student.is_verified:
        send_verification_email(db, student, request)
    # Always the same answer, so this cannot be used to enumerate who has registered.
    return templates.TemplateResponse(request, "resend.html", {"sent": True})


@router.get("/login", response_class=HTMLResponse)
def login_form(request: Request, pending: int = 0):
    note = ""
    if pending:
        note = "That account is not ready to sit yet. Confirm your email, or wait for approval."
    return templates.TemplateResponse(request, "login.html", {"errors": [], "note": note})


@router.post("/login")
def login(
    request: Request,
    email: str = Form(...),
    password: str = Form(...),
    db: Session = Depends(get_db),
):
    email = _normalise_email(email)
    student = db.scalar(select(Student).where(func.lower(Student.email) == email))
    if student is None or not verify_password(password, student.password_hash):
        logging_service.record(
            db,
            "LOGIN_FAILED",
            email,
            level="WARNING",
            student_id=student.id if student else None,
            request=request,
        )
        return templates.TemplateResponse(
            request,
            "login.html",
            {"errors": ["Email or password is incorrect."], "note": ""},
            status_code=401,
        )

    if student.is_blocked:
        logging_service.record(
            db, "LOGIN_BLOCKED", email, level="WARNING", student_id=student.id, request=request
        )
        return templates.TemplateResponse(
            request,
            "login.html",
            {"errors": ["This account has been suspended. Speak to your lecturer."], "note": ""},
            status_code=403,
        )
    if not student.is_verified:
        return templates.TemplateResponse(
            request,
            "login.html",
            {
                "errors": ["Confirm your email address first. Check your inbox for the link."],
                "note": "",
            },
            status_code=403,
        )
    if not student.is_approved:
        return templates.TemplateResponse(
            request,
            "login.html",
            {"errors": ["An administrator has not yet approved this account."], "note": ""},
            status_code=403,
        )

    student.last_login_at = datetime.utcnow()
    db.commit()
    logging_service.record(db, "LOGIN", email, student_id=student.id, request=request)

    response = RedirectResponse("/", status_code=303)
    return _set_session(response, {"role": "student", "id": student.id})


@router.get("/logout")
def logout(request: Request, db: Session = Depends(get_db)):
    response = RedirectResponse("/login", status_code=303)
    response.delete_cookie(settings.session_cookie)
    return response
