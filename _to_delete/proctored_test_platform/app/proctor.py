"""Proctoring rules, carried over from the desktop build and rewritten against the database.

The policy is warn-then-flag. An incident is recorded and shown to the candidate; when
the strike total reaches the threshold the attempt is marked FLAGGED. Nothing here ever
submits a paper on its own - only the clock does that.

A flag is a prompt for a human to look. It is not a finding, and the wording that
reaches the marker says so.
"""

from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from app.config import settings
from app.models import Attempt, Incident

# Categories that count towards the strike total. Blocked keystrokes and right-clicks
# are recorded for the record but do not escalate: they are noise, not evidence.
STRIKEABLE = {
    "FULLSCREEN_EXIT",
    "WINDOW_HIDDEN",
    "SCREENSHOT_KEY",
    "PHONE_DETECTED",
    "ABSENCE_FLAGGED",
    "MULTIPLE_FACES",
    "CLOCK_CHANGED",
    "CAMERA_STOPPED",
}

# Categories worth a snapshot and an email to the admin the moment they happen.
ALERTABLE = {"PHONE_DETECTED", "MULTIPLE_FACES", "ABSENCE_FLAGGED"}

INCIDENT_LABELS = {
    "FULLSCREEN_EXIT": "Left full-screen test mode",
    "WINDOW_HIDDEN": "Test window minimised or switched away from",
    "SCREENSHOT_KEY": "Screen-capture key pressed",
    "PHONE_DETECTED": "Phone-like device seen by the webcam",
    "ABSENCE_FLAGGED": "No candidate visible to the webcam",
    "ABSENCE_WARNED": "Candidate briefly out of camera view",
    "MULTIPLE_FACES": "More than one person visible to the webcam",
    "CAMERA_STOPPED": "Webcam stopped during the test",
    "CAMERA_UNAVAILABLE": "Webcam proctoring unavailable",
    "CLOCK_CHANGED": "Device clock moved backwards",
    "COPY_BLOCKED": "Copy or cut blocked",
    "PASTE_BLOCKED": "Paste blocked",
    "CONTEXT_MENU_BLOCKED": "Right-click menu blocked",
    "SHORTCUT_BLOCKED": "Blocked keyboard shortcut",
    "RECONNECTED": "Test page reloaded or reconnected",
    "NETWORK_LOST": "Connection to the server was lost",
}


def label_for(category: str) -> str:
    return INCIDENT_LABELS.get(category, category.replace("_", " ").capitalize())


def elapsed_seconds(attempt: Attempt, now: datetime | None = None) -> float:
    """Elapsed test time, ratcheted so it can only ever go up.

    The web version has an advantage the desktop one did not: the authoritative clock is
    the server's, and a candidate cannot touch it. The high-water mark is still kept, so
    a clock that jumps for any other reason - a container restart, a daylight change on a
    misconfigured host - cannot give time back either.
    """
    now = now or datetime.utcnow()
    wall = (now - attempt.started_at).total_seconds()
    return max(0.0, attempt.elapsed_high_water_seconds, wall)


def touch(attempt: Attempt, now: datetime | None = None) -> float:
    """Advance the ratchet and record that the candidate is still with us."""
    now = now or datetime.utcnow()
    elapsed = elapsed_seconds(attempt, now)
    if elapsed > attempt.elapsed_high_water_seconds:
        attempt.elapsed_high_water_seconds = elapsed
    attempt.last_seen_at = now
    return elapsed


def remaining_seconds(attempt: Attempt, now: datetime | None = None) -> int:
    total = attempt.exam.duration_minutes * 60
    return max(0, int(total - elapsed_seconds(attempt, now)))


def is_expired(attempt: Attempt, now: datetime | None = None) -> bool:
    return remaining_seconds(attempt, now) <= 0


def record_incident(
    db: Session,
    attempt: Attempt,
    category: str,
    detail: str = "",
    source: str = "browser",
    commit: bool = True,
) -> Incident:
    category = (category or "UNKNOWN").upper()[:40]
    counted = category in STRIKEABLE
    incident = Incident(
        attempt_id=attempt.id,
        category=category,
        label=label_for(category),
        detail=(detail or "")[:2000],
        source=source,
        counted=counted,
        occurred_at=datetime.utcnow(),
        elapsed_seconds=int(elapsed_seconds(attempt)),
    )
    db.add(incident)

    if counted:
        attempt.strike_count = (attempt.strike_count or 0) + 1
        if settings.strike_flag_after and attempt.strike_count >= settings.strike_flag_after:
            attempt.flagged = True

    if commit:
        db.commit()
        db.refresh(incident)
    return incident


def should_email_alert(attempt: Attempt, category: str, now: datetime | None = None) -> bool:
    """Rate-limit admin alerts.

    A restless candidate can trip the camera a dozen times in a minute. Emailing each one
    buries the message that matters, so alerts are throttled per attempt.
    """
    if category not in ALERTABLE:
        return False
    now = now or datetime.utcnow()
    last = attempt.last_alert_email_at
    if last is None:
        return True
    gap = timedelta(seconds=settings.snapshot_alert_min_interval_seconds)
    return now - last >= gap


def status_payload(attempt: Attempt, now: datetime | None = None) -> dict:
    """What the sitting page polls for once a second."""
    now = now or datetime.utcnow()
    return {
        "ok": True,
        "remaining_seconds": remaining_seconds(attempt, now),
        "elapsed_seconds": int(elapsed_seconds(attempt, now)),
        "strike_count": attempt.strike_count or 0,
        "flag_after": settings.strike_flag_after,
        "flagged": bool(attempt.flagged),
        "locked": bool(attempt.is_locked),
        "expired": is_expired(attempt, now),
    }


def client_settings() -> dict:
    """Thresholds the browser needs in order to apply the same rules locally."""
    return {
        "absence_warn_seconds": settings.absence_warn_seconds,
        "absence_flag_seconds": settings.absence_flag_seconds,
        "multiple_faces_seconds": settings.multiple_faces_seconds,
        "phone_confirm_seconds": settings.phone_confirm_seconds,
        "phone_warning_display_seconds": settings.phone_warning_display_seconds,
        "fullscreen_grace_seconds": settings.fullscreen_grace_seconds,
        "hidden_warn_seconds": settings.hidden_warn_seconds,
        "block_shortcut_keys": settings.block_shortcut_keys,
        "block_copy_paste": settings.block_copy_paste,
        "block_context_menu": settings.block_context_menu,
        "snapshots_enabled": settings.snapshots_enabled,
        "snapshot_max_width": settings.snapshot_max_width,
        "flag_after": settings.strike_flag_after,
    }
