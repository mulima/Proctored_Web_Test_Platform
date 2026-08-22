"""Persistent application log.

Everything the platform does that might later need explaining goes through here and
lands in the database, not in container stdout that Railway rotates away.
"""

from typing import Any

from sqlalchemy.orm import Session

from app.models import AppLog


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
    ip = ""
    user_agent = ""
    if request is not None:
        client = getattr(request, "client", None)
        # Railway sits behind a proxy, so the forwarded header is the real client.
        ip = (
            request.headers.get("x-forwarded-for", "").split(",")[0].strip()
            or (client.host if client else "")
        )[:64]
        user_agent = request.headers.get("user-agent", "")[:400]

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
