"""Timer rules: when a snooze is due, what happens next, and how `later` moves it.

Pure module: standard library, `store` and `echo` only. It decides; the timer
(`send.py`) does the sending. Times are aware UTC; `schedule.every` is in
minutes, as stored. See `docs/user-guide.md` ("Lifecycle and stored data").
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Literal
from zoneinfo import ZoneInfo

from . import echo
from .parse import next_on_days
from .store import ACTIVE, PENDING, Snooze

PENDING_TIMEOUT = timedelta(minutes=10)  # time the user has to reply `yes`
LATE_GRACE = timedelta(minutes=2)  # a reminder later than this says "late by"

NOTHING, SEND, MISS, EXPIRE = "nothing", "send", "miss", "expire"


@dataclass(frozen=True)
class Action:
    """What the timer must do with a snooze. `late_by` is set only for a late `send`."""

    kind: Literal["nothing", "send", "miss", "expire"]
    late_by: timedelta | None = None


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


def is_recurring(snooze: Snooze) -> bool:
    """Whether this snooze has at least one recurrence day."""
    return bool(snooze.schedule.days)


def rearm(snooze: Snooze, now: datetime) -> datetime:
    """Start the next occurrence of a recurring snooze and reset its reminder state."""
    if not is_recurring(snooze):
        raise ValueError("cannot re-arm a one-time snooze")

    old_first = echo.from_iso(snooze.schedule.first_at)
    if old_first is None:
        raise ValueError("recurring snooze has an invalid first_at")
    zone = ZoneInfo(snooze.schedule.tz)
    local_first = old_first.astimezone(zone)
    after = max(now, old_first)
    new_first = next_on_days(
        local_first.timetz().replace(tzinfo=None),
        after,
        zone,
        frozenset(snooze.schedule.days),
    )

    old_until = echo.from_iso(snooze.schedule.until)
    if snooze.schedule.span is not None:
        span = timedelta(minutes=snooze.schedule.span)
    elif old_until is not None:
        span = old_until - old_first
    else:
        span = None

    snooze.schedule.first_at = _iso(new_first)
    snooze.schedule.until = _iso(new_first + span) if span is not None else None
    snooze.next_at = _iso(new_first)
    snooze.state = ACTIVE
    snooze.sent_count = 0
    snooze.last_sent_at = None
    snooze.last_message_id = None
    snooze.last_error = None
    return new_first


# -- pending snoozes: the `yes` window -----------------------------------------


def pending_deadline(snooze: Snooze) -> datetime | None:
    """When a pending snooze stops waiting for `yes` (creation + `PENDING_TIMEOUT`); None if `created` is unreadable."""
    created = echo.from_iso(snooze.created)
    return created + PENDING_TIMEOUT if created else None


def is_pending_expired(snooze: Snooze, now: datetime) -> bool:
    """True if `snooze` is still pending and its `yes` window has closed at `now`."""
    if snooze.state != PENDING:
        return False
    deadline = pending_deadline(snooze)
    return deadline is not None and now >= deadline


# -- active snoozes: send, miss, wait ------------------------------------------


def due(snooze: Snooze, now: datetime, cap_reminders: int) -> Action:
    """The action for `snooze` at `now`."""
    if snooze.state == PENDING:
        return Action(EXPIRE) if is_pending_expired(snooze, now) else Action(NOTHING)
    next_at = echo.from_iso(snooze.next_at)
    if snooze.state != ACTIVE or next_at is None:
        return Action(NOTHING)
    until = echo.from_iso(snooze.schedule.until)
    if until is not None and now >= until:
        return Action(MISS)
    if now < next_at:
        return Action(NOTHING)
    if snooze.sent_count >= cap_reminders:
        return Action(MISS)  # the user had one interval to answer the last reminder
    late = now - next_at
    return Action(SEND, late if late > LATE_GRACE else None)


def after_send(snooze: Snooze, sent_at: datetime) -> None:
    """Record a sent reminder. Next one: `every` after the send (a long sleep gives one late reminder, not a burst)."""
    snooze.sent_count += 1
    snooze.last_sent_at = _iso(sent_at)
    snooze.next_at = _iso(sent_at + timedelta(minutes=snooze.schedule.every))
    snooze.last_error = None


def apply_later(snooze: Snooze, now: datetime, minutes: float) -> datetime:
    """Push the next reminder back by `minutes`; return the new time.

    Counted from now, except before the first reminder (nothing sent yet): then from the
    first reminder's time, so `later` never moves it earlier.

    A time past `until` moves `until` to one `every` after it: `due` misses at `until`, so the
    extra reminder is sent and the user still has one interval to answer it.
    """
    current = echo.from_iso(snooze.next_at)
    base = now
    if snooze.sent_count == 0 and current is not None and current > now:
        base = current
    at = base + timedelta(minutes=minutes)
    snooze.next_at = _iso(at)
    until = echo.from_iso(snooze.schedule.until)
    if until is not None and at > until:
        snooze.schedule.until = _iso(at + timedelta(minutes=snooze.schedule.every or 1))
    return at
