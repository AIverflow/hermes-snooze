"""Text shown to the user: local times, durations and the echo after a snooze is created."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone, tzinfo
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .store import PENDING, Snooze

# Where a snooze goes: the chat the command came from, or the profile's home channel.
WHERE_CHAT = "this chat"
WHERE_HOME = "home channel"

PLATFORM_LABELS = {"whatsapp": "WhatsApp", "whatsapp_cloud": "WhatsApp Cloud", "sms": "SMS"}

# `/snooze test`: a snooze flagged `test` (see `docs/user-guide.md`, "Commands").
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


def days_text(days: Any) -> str:
    """Render weekday numbers as a short recurrence label."""
    day_set = set(days or ())
    if day_set == set(range(7)):
        return "daily"
    if day_set == set(range(5)):
        return "weekdays"
    names = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
    return ", ".join(names[day] for day in range(7) if day in day_set)


def platform_label(platform: str) -> str:
    return PLATFORM_LABELS.get(platform.lower(), platform.title())


def default_platform_text(setting: str) -> str:
    """The `notification_platform` setting in words: "the chat where you type (source)" or "Discord (home channel)"."""
    if setting == "source":
        return "the chat where you type (source)"
    return f"{platform_label(setting)} (home channel)"


def choice_options(rule: Any) -> list[str]:
    """The options of a choice confirm rule, in order; [] for any other rule."""
    if not isinstance(rule, dict) or rule.get("kind") != "choice":
        return []
    return [option.strip() for option in str(rule.get("value", "")).split("|") if option.strip()]


def numbered(options: list[str]) -> str:
    """["a", "b"] -> "1 a | 2 b"."""
    return " | ".join(f"{index} {option}" for index, option in enumerate(options, 1))


def _stop_text(rule: Any) -> str:
    kind = rule.get("kind") if isinstance(rule, dict) else None
    value = rule.get("value", "") if isinstance(rule, dict) else ""
    if kind == "choice":
        return "reply with one of: " + numbered(choice_options(rule))
    if kind == "word":
        return f'reply "{value}"'
    if kind == "contains":
        return f'reply with a message containing "{value}"'
    if kind == "any":
        return "reply with any message"
    if kind == "emoji":
        return f"send {value}"
    return 'reply "Ok"'


def render_echo(
    snooze: Snooze,
    now: datetime,
    where: str,
    cap_reminders: int,
    yes_window: timedelta,
    gateway_off: bool = False,
) -> str:
    """What the user sees after creating a snooze: local times, where, how to stop, how to undo.

    A pending snooze gets one "Waiting" line. In its own chat (`where` is WHERE_CHAT) the user can send a
    bare `yes`; elsewhere (CLI, Desktop, home channel) the line gives `/snooze yes <id>`.
    """
    sched = snooze.schedule
    first = from_iso(sched.first_at)
    until = from_iso(sched.until)
    zone = sched.tz
    pending = snooze.state == PENDING

    if first and sched.days:
        local_time = first.astimezone(zone_of(zone)).strftime("%H:%M").lstrip("0")
        when = [f"{days_text(sched.days)} at {local_time}", f"next {format_when(first, zone, now)}"]
    else:
        when = [format_when(first, zone, now)] if first else []
    if sched.every:
        when.append(f"every {format_minutes(sched.every)}")
    if until:
        stop = f"stops {format_when(until, zone, now, relative_to=first)} or after {cap_reminders} reminders"
        when.append(stop)

    lines = [
        f"⏰ Snooze #{snooze.id} {'ready, not started' if pending else 'set'}",
        f"What:     {snooze.text}",
        f"When:     {', '.join(when)} ({zone})",
        f'Stop it:  {_stop_text(snooze.confirm)} | "later 30" to push it back',
    ]
    if snooze.then:
        lines.append(f'Then:     the agent will "{snooze.then}" and tell you the result')
    lines.append(f"Where:    {platform_label(snooze.target.platform)} ({where})")
    if choice_options(snooze.confirm):
        lines.append("Answer in chat: reply with an option")
    else:
        lines.append(f'If "Ok" does not match: /snooze ok {snooze.id}')
    lines.append(f"Undo:     /snooze cancel {snooze.id}")
    if pending:
        minutes = format_minutes(minutes_value(yes_window))
        if where == WHERE_CHAT:
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
            f"({platform_label(snooze.target.platform)}, {where}): reply `Ok`.",
            f"No reply within {format_minutes(minutes_value(TEST_WINDOW))}: the test fails.",
        ]
    )


def _ordinal(n: int) -> str:
    """2 -> "2nd", 3 -> "3rd", 11 -> "11th"."""
    suffix = "th" if n % 100 in (11, 12, 13) else {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


def reminder_text(snooze: Snooze, late_by: timedelta | None = None) -> str:
    """Every reminder shows its snooze id; the first one says how to answer."""
    count = snooze.sent_count + 1
    options = choice_options(snooze.confirm)
    instruction = f"reply 1-{len(options)}" if options else _stop_text(snooze.confirm)
    tail = f"  ({instruction} | later)" if count == 1 else f" ({_ordinal(count)})"
    text = f"🔔 Snooze #{snooze.id}: {snooze.text}{tail}"
    if late_by is not None:
        text += f"\n⏰ late by {format_minutes(round(late_by.total_seconds() / 60))}"
    if options:
        text += "\n" + numbered(options)
    return text


def missed_text(snooze: Snooze) -> str:
    """The message when a snooze ends without an answer."""
    if snooze.test:
        return TEST_MISSED
    n = snooze.sent_count
    return f"⌛ Missed: {snooze.text} ({n} reminder{'' if n == 1 else 's'}, no answer)"
