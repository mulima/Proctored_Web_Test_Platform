"""Conversion helpers for Exam.scheduled_start_at.

Every other datetime in this app (Attempt.started_at, deadline_at, ...) is naive
UTC - the server clock is the one authority, and nothing here should introduce a
second convention. An admin, though, thinks in their own local time when scheduling
a start ("9am on the 25th"), so the settings form collects a wall-clock date/time
plus an IANA zone name and this module converts between that and the stored UTC
instant. scheduled_start_timezone is kept purely so the form can show the admin
back what they originally typed, in the zone they typed it in - not because
anything else in the app needs it.
"""

from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError, available_timezones

# Sorted once at import time - available_timezones() does a filesystem/zip scan.
ALL_TIMEZONES: list[str] = sorted(available_timezones())

_UTC = ZoneInfo("UTC")


def local_to_utc(datetime_local_value: str, tz_name: str) -> datetime:
    """Combines an HTML <input type="datetime-local"> value ('YYYY-MM-DDTHH:MM')
    and an IANA zone name into the naive UTC datetime Exam.scheduled_start_at
    stores. Raises ValueError for a malformed datetime, ZoneInfoNotFoundError for
    an unrecognised zone - callers should catch both.
    """
    naive = datetime.strptime(datetime_local_value, "%Y-%m-%dT%H:%M")
    aware = naive.replace(tzinfo=ZoneInfo(tz_name))
    return aware.astimezone(_UTC).replace(tzinfo=None)


def utc_to_local_input(value: datetime, tz_name: str) -> str:
    """The reverse - a stored naive UTC datetime, formatted as a datetime-local
    input value in the given zone, so re-opening the settings form shows what was
    originally entered rather than a UTC time that reads oddly against the zone
    name sitting right next to it.
    """
    aware_utc = value.replace(tzinfo=_UTC)
    local = aware_utc.astimezone(ZoneInfo(tz_name))
    return local.strftime("%Y-%m-%dT%H:%M")


def format_for_display(value: datetime, tz_name: str) -> str:
    """A human-readable rendering for the student dashboard, e.g.
    '25 Sep 2026, 09:00 Africa/Lusaka'. Falls back to UTC rather than raising if
    tz_name is no longer a recognised IANA zone (e.g. a retired name) - this runs
    on the student-facing dashboard and must never be the reason that page 500s.
    """
    try:
        zone = ZoneInfo(tz_name)
    except ZoneInfoNotFoundError:
        zone = _UTC
        tz_name = "UTC"
    aware_utc = value.replace(tzinfo=_UTC)
    local = aware_utc.astimezone(zone)
    return f"{local.strftime('%d %b %Y, %H:%M')} {tz_name}"
