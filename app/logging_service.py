"""Persistent application log.

Everything a course does that might later need explaining goes through `record`
and lands in that lecturer's own database (app_logs), not in container stdout
that Railway rotates away. `record_platform` is the same idea one level up -
signups, logins, database-connection attempts - and always lands in the
platform database (platform_logs), regardless of which course a request is for.
"""

from typing import Any

from sqlalchemy.orm import Session

from app.models_course import AppLog
from app.models_platform import PlatformLog


def _request_ip_and_agent(request: Any) -> tuple[str, str]:
    if request is None:
        return "", ""
    client = getattr(request, "client", None)
    # Railway sits behind a proxy, so the forwarded header is the real client.
    ip = (
        request.headers.get("x-forwarded-for", "").split(",")[0].strip()
        or (client.host if client else "")
    )[:64]
    user_agent = request.headers.get("user-agent", "")[:400]
    return ip, user_agent


def record(
    db: Session,
    event_type: str,
    message: str = "",
    *,
    level: str = "INFO",
    student_id: int | None = None,
    attempt_id: int | None = None,
    request: Any = None,
    payload: dict | None = None,
    commit: bool = True,
) -> AppLog:
    ip, user_agent = _request_ip_and_agent(request)
    entry = AppLog(
        event_type=event_type[:60],
        message=message[:4000],
        level=level,
        student_id=student_id,
        attempt_id=attempt_id,
        ip=ip,
        user_agent=user_agent,
        payload=payload,
    )
    db.add(entry)
    if commit:
        db.commit()
    return entry


def record_platform(
    db: Session,
    event_type: str,
    message: str = "",
    *,
    level: str = "INFO",
    lecturer_id: int | None = None,
    request: Any = None,
    payload: dict | None = None,
    commit: bool = True,
) -> PlatformLog:
    ip, user_agent = _request_ip_and_agent(request)
    entry = PlatformLog(
        event_type=event_type[:60],
        message=message[:4000],
        level=level,
        lecturer_id=lecturer_id,
        ip=ip,
        user_agent=user_agent,
        payload=payload,
    )
    db.add(entry)
    if commit:
        db.commit()
    return entry
