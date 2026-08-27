"""Non-blocking operational alerts and durable monitoring events."""

import logging
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import settings
from app.mailer import Message, send

logger = logging.getLogger("app.monitoring")


def notify_operator(subject: str, message: str) -> None:
    if not settings.alert_email:
        logger.error("ALERT %s: %s", subject, message)
        return
    delivered = send(Message(to=settings.alert_email, subject=subject, body=message))
    if not delivered:
        logger.error("ALERT delivery failed: %s: %s", subject, message)


def platform_alert(db: Session | None, event_type: str, message: str, *, request: Any = None, payload: dict | None = None) -> None:
    if db is not None:
        try:
            from app import logging_service

            logging_service.record_platform(db, event_type, message, level="ERROR", request=request, payload=payload)
        except Exception:
            logger.exception("Could not persist platform alert %s", event_type)
    notify_operator(f"ClearGrade alert: {event_type}", message)


def course_alert(db: Session, event_type: str, message: str, *, request: Any = None, payload: dict | None = None) -> None:
    try:
        from app import logging_service

        logging_service.record(db, event_type, message, level="ERROR", request=request, payload=payload)
    except Exception:
        logger.exception("Could not persist course alert %s", event_type)
    notify_operator(f"ClearGrade alert: {event_type}", message)


def repeated_platform_event(db: Session, event_type: str, *, request: Any = None, message: str = "") -> None:
    from app.models_platform import PlatformLog

    since = datetime.utcnow() - timedelta(minutes=settings.alert_window_minutes)
    count = db.scalar(select(func.count(PlatformLog.id)).where(PlatformLog.event_type == event_type, PlatformLog.at >= since)) or 0
    if count >= settings.alert_threshold and count % settings.alert_threshold == 0:
        platform_alert(db, f"ALERT_{event_type}", message or f"{count} {event_type} events in the last {settings.alert_window_minutes} minutes.", request=request, payload={"count": count, "window_minutes": settings.alert_window_minutes})


def repeated_course_event(db: Session, event_type: str, *, request: Any = None, message: str = "") -> None:
    from app.models_course import AppLog

    since = datetime.utcnow() - timedelta(minutes=settings.alert_window_minutes)
    count = db.scalar(select(func.count(AppLog.id)).where(AppLog.event_type == event_type, AppLog.at >= since)) or 0
    if count >= settings.alert_threshold and count % settings.alert_threshold == 0:
        course_alert(db, f"ALERT_{event_type}", message or f"{count} {event_type} events in the last {settings.alert_window_minutes} minutes.", request=request, payload={"count": count, "window_minutes": settings.alert_window_minutes})