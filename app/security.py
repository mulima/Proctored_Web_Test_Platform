"""Password hashing, signed cookies and single-use email tokens."""

import hmac
import secrets
from datetime import datetime, timedelta
from typing import Any

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerifyMismatchError
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from app.config import settings

_hasher = PasswordHasher()

SESSION_SALT = "exam.session"
VERIFY_SALT = "exam.verify"
LECTURER_VERIFY_SALT = "platform.lecturer.verify"


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password: str, password_hash: str) -> bool:
    try:
        return _hasher.verify(password_hash, password)
    except (VerifyMismatchError, InvalidHashError, Exception):
        return False


def needs_rehash(password_hash: str) -> bool:
    try:
        return _hasher.check_needs_rehash(password_hash)
    except Exception:
        return False


def _serializer(salt: str) -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(settings.secret_key, salt=salt)


def make_session_cookie(payload: dict[str, Any]) -> str:
    return _serializer(SESSION_SALT).dumps(payload)


def read_session_cookie(token: str | None) -> dict[str, Any] | None:
    if not token:
        return None
    try:
        return _serializer(SESSION_SALT).loads(
            token, max_age=settings.session_max_age_seconds
        )
    except (BadSignature, SignatureExpired):
        return None


def set_session_cookie(response, *, role: str, slug: str, id: int) -> None:
    """The one place a session cookie gets written, for students and lecturers alike.

    The payload carries `slug` alongside `role`/`id` because under multi-tenancy a bare
    numeric id means nothing on its own - it's only meaningful within one specific
    lecturer's database. `current_student`/`current_lecturer` (app/deps.py) reject a
    cookie whose slug doesn't match the URL being requested, same as an invalid
    signature. The cookie is also scoped with Path=/{slug} as defense in depth, so a
    browser won't even attach one course's cookie to another course's requests.
    """
    response.set_cookie(
        settings.session_cookie,
        make_session_cookie({"role": role, "slug": slug, "id": id}),
        max_age=settings.session_max_age_seconds,
        httponly=True,
        samesite="lax",
        secure=settings.base_url.startswith("https"),
        path=f"/{slug}",
    )


def make_verification_token(email: str, slug: str) -> str:
    """Issue a verification token, scoped to one course.

    The nonce matters. itsdangerous stamps the payload with a whole-second timestamp, so
    without it two links issued for the same address inside the same second come out
    byte-identical - and a resend would not actually supersede the link it replaced.
    With it, every issued link is distinct, and because only the newest is stored on the
    student row, following an older one fails.

    The slug matters too: the same email address could genuinely register with two
    different lecturers' courses, in two different databases. A token only proves "this
    email was issued a link for *this* course."
    """
    return _serializer(VERIFY_SALT).dumps(
        {"email": email.lower(), "slug": slug, "n": secrets.token_urlsafe(9)}
    )


def read_verification_token(token: str, slug: str) -> str | None:
    """Returns the email the token was issued for, or None if bad, expired, or
    issued for a different course."""
    try:
        data = _serializer(VERIFY_SALT).loads(
            token, max_age=settings.verification_token_max_age_seconds
        )
    except (BadSignature, SignatureExpired):
        return None
    data = data or {}
    if data.get("slug") != slug:
        return None
    return data.get("email")


def make_lecturer_verification_token(email: str) -> str:
    """Same shape as make_verification_token, but for the platform's own lecturer
    accounts - not scoped to a slug, because a lecturer's email is globally unique
    across the whole platform (unlike a student's, which only has to be unique
    within one course's own database), so there's no cross-course ambiguity to
    guard against. A distinct salt keeps a lecturer token from ever being replayed
    as a student one or vice versa."""
    return _serializer(LECTURER_VERIFY_SALT).dumps(
        {"email": email.lower(), "n": secrets.token_urlsafe(9)}
    )


def read_lecturer_verification_token(token: str) -> str | None:
    try:
        data = _serializer(LECTURER_VERIFY_SALT).loads(
            token, max_age=settings.verification_token_max_age_seconds
        )
    except (BadSignature, SignatureExpired):
        return None
    return (data or {}).get("email")


def tokens_match(stored: str | None, presented: str) -> bool:
    """Constant-time compare, so a wrong token cannot be discovered by timing."""
    if not stored:
        return False
    return hmac.compare_digest(stored, presented)


WEAK_PASSWORDS = {"password12", "1234567890", "qwertyuiop", "letmein123", "changeme12"}


def password_problems(password: str, course_code: str = "") -> list[str]:
    """Deliberately modest rules: long enough to matter, nothing that invites Password1!"""
    problems = []
    squashed = password.lower().replace(" ", "")
    if len(password) < 10:
        problems.append("Use at least 10 characters.")
    if password.isdigit():
        problems.append("Do not use only numbers.")
    if squashed in WEAK_PASSWORDS:
        problems.append("That password is too easy to guess.")
    # The course code is the first thing anyone would try for this particular site.
    code = (course_code or "").lower()
    if code and len(code) >= 4 and code in squashed:
        problems.append("Do not build your password out of the course code.")
    return problems


def utcnow() -> datetime:
    return datetime.utcnow()


def deadline_from(started_at: datetime, duration_minutes: int) -> datetime:
    return started_at + timedelta(minutes=duration_minutes)
