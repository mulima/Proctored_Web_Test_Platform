"""In-process pub/sub for the admin live event feed (Server-Sent Events).

Deliberately in-memory, not database-backed - the same single-process assumption
tenant_db.py's engine cache already makes (see its own module docstring). This
would need a real broker (Redis pub/sub, etc.) the day this platform ever runs more
than one worker process/replica, since a broadcast in one process's memory never
reaches a browser connected to a different one.

Keyed by (course slug, exam id), NOT exam id alone: exam ids are only unique within
one course's own database (app/tenant_db.py - every course has its own schema or
database), so two different courses can each have an exam with id=1. Keying on the
exam id alone would leak one course's live incident feed to another course's admin
whenever their exam ids happened to collide.
"""

import asyncio

_Key = tuple[str, int]

_subscribers: dict[_Key, list[asyncio.Queue]] = {}


def subscribe(slug: str, exam_id: int) -> asyncio.Queue:
    queue: asyncio.Queue = asyncio.Queue()
    _subscribers.setdefault((slug, exam_id), []).append(queue)
    return queue


def unsubscribe(slug: str, exam_id: int, queue: asyncio.Queue) -> None:
    key = (slug, exam_id)
    listeners = _subscribers.get(key)
    if not listeners:
        return
    if queue in listeners:
        listeners.remove(queue)
    if not listeners:
        _subscribers.pop(key, None)


def publish(slug: str, exam_id: int, event: dict) -> None:
    """Best-effort - never raises. Called from the same request that just recorded
    the underlying incident/message; a delivery problem here must never fail that
    request or lose the already-persisted record, only the live notification of it."""
    for queue in _subscribers.get((slug, exam_id), []):
        try:
            queue.put_nowait(event)
        except Exception:
            pass
