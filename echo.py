"""Text shown to the user: local times, durations and the echo after a snooze is created."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone, tzinfo
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .store import PENDING, Snooze

PLATFORM_LABELS = {"whatsapp": "WhatsApp", "whatsapp_cloud": "WhatsApp Cloud", "sms": "SMS"}

# `/snooze test`: a snooze flagged `test` (see README, "Commands").
TEST_TEXT = "🧪 Snooze test"
TEST_WINDOW = timedelta(minutes=2)  # time the user has to reply
TEST_ARRIVES_IN = 15  # seconds; the timer's tick (`send.TICK_SECONDS`)
TEST_PASSED = "✅ Test passed: reminders are sent and your replies are received."
TEST_MISSED = (
    "❌ Test: no reply received in 2 min. Check that the gateway serves this profile and that you replied in this chat."
)
TEST_GATEWAY_OFF = (
    "🧪 Snooze test not started\n"
    "❌ Gateway not running for this profile: it could send nothing and read no reply. "
    "Start it (hermes gateway start), then run /snooze test again."
)
GATEWAY_OFF_WARNING = (
    "⚠️ The gateway is not running for this profile: no reminder will be sent until it runs (hermes gateway start)."
)


def from_iso(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def zone_of(name: str) -> tzinfo:
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, OSError):
        return timezone.utc


def minutes_value(delta: timedelta) -> float:
    """A timedelta as minutes: an int when whole, else a float."""
    minutes = delta.total_seconds() / 60
    return int(minutes) if minutes == int(minutes) else round(minutes, 4)


def format_minutes(minutes: Any) -> str:
    """10 -> "10 min", 60 -> "1 h", 90 -> "1 h 30 min"."""
    total = round(float(minutes) * 60)
    hours, rest = divmod(total, 3600)
    mins, secs = divmod(rest, 60)
    parts = []
    if hours:
        parts.append(f"{hours} h")
    if mins:
        parts.append(f"{mins} min")
    if secs:
        parts.append(f"{secs} s")
    return " ".join(parts) or "0 min"


def format_when(moment: datetime, zone_name: str, now: datetime, relative_to: datetime | None = None) -> str:
    """Local time of `moment`: "today 16:00", "tomorrow 08:00", "Sat 8 Nov 08:00".

    With `relative_to`, a moment on the same local day is just "16:00".
    """
    zone = zone_of(zone_name)
    local = moment.astimezone(zone)
    clock = f"{local:%H:%M}"
    day = local.date()
    if relative_to is not None and relative_to.astimezone(zone).date() == day:
        return clock
    today = now.astimezone(zone).date()
    if day == today:
        return f"today {clock}"
    if day == today + timedelta(days=1):
        return f"tomorrow {clock}"
    return f"{local:%a} {local.day} {local:%b} {clock}"


def platform_label(platform: str) -> str:
    return PLATFORM_LABELS.get(platform.lower(), platform.title())


def _stop_text(rule: Any) -> str:
    kind = rule.get("kind") if isinstance(rule, dict) else None
    value = rule.get("value", "") if isinstance(rule, dict) else ""
    if kind == "word":
        return f'reply "{value}"'
    if kind == "contains":
        return f'reply with a message containing "{value}"'
    if kind == "any":
        return "reply with any message"
    if kind == "emoji":
        return f"send {value} (a message or a reaction)"
    return 'reply "ok"'


def render_echo(
    snooze: Snooze,
    now: datetime,
    where: str,
    capped: bool,
    cap_reminders: int,
    yes_window: timedelta,
    gateway_off: bool = False,
    from_chat: bool = False,
) -> str:
    """What the user sees after creating a snooze: local times, where, how to stop, how to undo.

    A pending snooze gets one "Waiting" line. `from_chat` picks the wording: a chat app can send a bare `yes`,
    the CLI and Desktop cannot, so they get `/snooze yes <id>`.
    """
    sched = snooze.schedule
    first = from_iso(sched.first_at)
    until = from_iso(sched.until)
    zone = sched.tz
    pending = snooze.state == PENDING

    when = [format_when(first, zone, now)] if first else []
    if sched.every:
        when.append(f"every {format_minutes(sched.every)}")
    if until:
        stop = f"stops {format_when(until, zone, now, relative_to=first)}"
        if capped:
            stop += f" or after {cap_reminders} reminders"
        when.append(stop)

    lines = [
        f"⏰ Snooze #{snooze.id} {'ready, not started' if pending else 'set'}",
        f"What:     {snooze.text}",
        f"When:     {', '.join(when)} ({zone})",
        f'Stop it:  {_stop_text(snooze.confirm)} (or "later 30" to push it back)',
    ]
    if snooze.then:
        lines.append(f'Then:     the agent will "{snooze.then}" and tell you the result')
    lines.append(f"Where:    {platform_label(snooze.target.platform)} ({where})")
    lines.append(f"Undo:     /snooze cancel {snooze.id}")
    if pending:
        minutes = format_minutes(minutes_value(yes_window))
        if from_chat:
            lines.append(f"Waiting:  not started. Reply `yes` within {minutes}.")
        else:
            lines.append(
                f"Waiting:  not started. Type `/snooze yes {snooze.id}` within {minutes} "
                "(a bare `yes` only works in chat apps)."
            )
    if gateway_off:
        lines.append(GATEWAY_OFF_WARNING)
    return "\n".join(lines)


def test_started_text(snooze: Snooze, where: str, gateway_running: bool | None) -> str:
    """The reply to `/snooze test`: the check that can be done now, and what to do next."""
    if gateway_running is None:
        check = "❔ Gateway: could not check whether it runs for this profile"
    else:
        check = "✅ Gateway running for this profile" if gateway_running else "❌ Gateway not running for this profile"
    return "\n".join(
        [
            f"🧪 Snooze test #{snooze.id}",
            check,
            f"A test reminder will arrive in the next {TEST_ARRIVES_IN} s "
            f"({platform_label(snooze.target.platform)}, {where}): reply `ok`.",
            f"No reply within {format_minutes(minutes_value(TEST_WINDOW))}: the test fails.",
        ]
    )


def _ordinal(n: int) -> str:
    """2 -> "2nd", 3 -> "3rd", 11 -> "11th"."""
    suffix = "th" if n % 100 in (11, 12, 13) else {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


def reminder_text(snooze: Snooze, late_by: timedelta | None = None) -> str:
    """The reminder message. The first one says how to answer; later ones say which number they are."""
    count = snooze.sent_count + 1
    tail = "  (reply ok / later)" if count == 1 else f" ({_ordinal(count)})"
    text = f"🔔 {snooze.text}{tail}"
    if late_by is not None:
        text += f"\n⏰ late by {format_minutes(round(late_by.total_seconds() / 60))}"
    return text


def missed_text(snooze: Snooze) -> str:
    """The message when a snooze ends without an answer."""
    if snooze.test:
        return TEST_MISSED
    n = snooze.sent_count
    return f"⌛ Missed: {snooze.text} ({n} reminder{'' if n == 1 else 's'}, no answer)"
