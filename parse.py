"""Turn the text after ``/snooze`` into a ``Spec`` (or a ``ParseError``).

Pure module: standard library only, no Hermes imports. The grammar is
documented in `docs/user-guide.md` ("Create a snooze"). ``parse`` returns the error
instead of raising it.
"""

import re
from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
from datetime import tzinfo as _tzinfo
from zoneinfo import ZoneInfo

from .confirm import normalize

MIN_DURATION = timedelta(minutes=1)
MAX_DURATION = timedelta(days=3650)  # about 10 years: far past any real reminder, far below datetime's limit

# The platforms `via <platform>` accepts. `commands.py` checks the setting and the tool against it too.
PLATFORMS = ("telegram", "discord")

_KEYWORDS = frozenset({"at", "in", "every", "until", "ok", "via", "then"})
_PREPOSITIONS = frozenset({"before", "by", "after", "around", "since", "from", "to"})
_TIME_CLAUSES = frozenset({"at", "until"})
_DURATION_CLAUSES = frozenset({"in", "every"})
# Spoken numbers: only used to word an error, never accepted as a duration.
_SPOKEN_NUMBERS = frozenset(
    {"one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten", "eleven", "twelve", "half"}
)
_DAY_NAMES = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
_DAYS = {name: day for day, name in enumerate(_DAY_NAMES)} | {name[:3]: day for day, name in enumerate(_DAY_NAMES)}

_TIME_RE = re.compile(r"^(\d{1,2})(?::(\d{2}))?(am|pm)?$")
# `\s*` lets a duration be one word ("2m") or words joined by `_join_number_units` ("1 h 30 min").
_DURATION_RE = re.compile(
    r"^(?:\d+\s*(?:hours|hour|hrs|hr|h|minutes|minute|mins|min|m|"
    r"seconds|second|secs|sec|s)\s*)+$"
)
_DURATION_PART_RE = re.compile(r"(\d+)\s*(hours|hour|hrs|hr|h|minutes|minute|mins|min|m|seconds|second|secs|sec|s)")
_UNIT_SECONDS = {
    "s": 1,
    "sec": 1,
    "secs": 1,
    "second": 1,
    "seconds": 1,
    "m": 60,
    "min": 60,
    "mins": 60,
    "minute": 60,
    "minutes": 60,
    "h": 3600,
    "hr": 3600,
    "hrs": 3600,
    "hour": 3600,
    "hours": 3600,
}

EXAMPLE_FULL = "/snooze Send email to John Doe at 4pm every 10m until 6pm then close ticket 42"
EXAMPLE_SHORT = "/snooze Wake up 8am 10m"
EXAMPLE_LONG = "/snooze Wake up at 8am every 10m"
EXAMPLE_IN = "/snooze Take my pill in 30m every 5m ok taken"
EXAMPLE_RECURRING = "/snooze Take my pill at 8am daily"
EXAMPLE_WEEKLY = "/snooze Water the plants at 8am every monday,thursday"
EXAMPLE_ON_DAY = "/snooze Call the dentist at 9am on monday"
EXAMPLE_VIA = "/snooze Water the plants at 8am daily via discord"


@dataclass(frozen=True)
class ConfirmRule:
    kind: str = "ok"
    value: str = ""


@dataclass(frozen=True)
class Spec:
    text: str
    first_at: datetime
    every: timedelta | None
    until: datetime | None
    confirm: ConfirmRule
    then: str | None
    tz: str
    days: frozenset[int] | None = None
    platform: str | None = None  # `via <platform>`: send to that platform's home channel


@dataclass(frozen=True)
class ParseError:
    message: str
    example: str

    def __str__(self) -> str:
        return f"{self.message} Example: {self.example}"


def _parse_time(word: str) -> time | None:
    m = _TIME_RE.match(word.casefold())
    if not m:
        return None
    hour, minute, ampm = int(m.group(1)), m.group(2), m.group(3)
    if minute is None and ampm is None:
        return None  # a bare number is not a time
    minute = int(minute) if minute is not None else 0
    if minute > 59:
        return None
    if ampm:
        if not 1 <= hour <= 12:
            return None
        hour = hour % 12 + (12 if ampm == "pm" else 0)
    elif hour > 23:
        return None
    return time(hour, minute)


def parse_duration(word: str) -> timedelta | None:
    """``10m``, ``1h30m``, ``90s`` as a timedelta; None if ``word`` is not a duration."""
    word = word.casefold()
    if not _DURATION_RE.match(word):
        return None
    try:
        seconds = sum(int(n) * _UNIT_SECONDS[u] for n, u in _DURATION_PART_RE.findall(word))
        return timedelta(seconds=seconds)
    except (OverflowError, ValueError):  # a huge number (ValueError: over 4300 digits)
        return timedelta.max  # callers reject anything above MAX_DURATION


def _join_number_units(toks: list[tuple[str, int, int]], text: str) -> list[tuple[str, int, int]]:
    """Merge number and unit words (``2``, ``min``) into one token, so ``2 min`` reads like ``2min``.

    Consecutive pairs merge too (``1 h 30 min``). The merged token keeps the original text between the
    words, so the reminder text can still be cut from ``text`` by position. A number not followed by a
    unit word is left alone.
    """
    out: list[tuple[str, int, int]] = []
    last_was_pair = False
    i = 0
    while i < len(toks):
        if i + 1 < len(toks) and toks[i][0].isdecimal() and toks[i + 1][0].casefold() in _UNIT_SECONDS:
            start = out.pop()[1] if last_was_pair else toks[i][1]
            out.append((text[start : toks[i + 1][2]], start, toks[i + 1][2]))
            last_was_pair = True
            i += 2
        else:
            out.append(toks[i])
            last_was_pair = False
            i += 1
    return out


def _next_occurrence(t: time, after: datetime, zone: _tzinfo) -> datetime:
    """First moment (UTC) at wall-clock time ``t`` in ``zone`` strictly after ``after``."""
    day = after.astimezone(zone).date()
    for _ in range(3):
        candidate = datetime.combine(day, t, tzinfo=zone).astimezone(timezone.utc)
        if candidate > after:
            return candidate
        day += timedelta(days=1)
    raise AssertionError("unreachable")  # pragma: no cover


def parse_days(word: str) -> frozenset[int] | None:
    """Parse weekday names (``mon`` or ``monday``), lists, and non-wrapping ranges."""
    days: set[int] = set()
    for part in word.casefold().split(","):
        if "-" in part:
            if part.count("-") != 1:
                return None
            first, last = part.split("-")
            if first not in _DAYS or last not in _DAYS or _DAYS[first] > _DAYS[last]:
                return None
            days.update(range(_DAYS[first], _DAYS[last] + 1))
        else:
            if part not in _DAYS:
                return None
            days.add(_DAYS[part])
    return frozenset(days) if days else None


def next_on_days(t: time, after: datetime, zone: _tzinfo, days: frozenset[int]) -> datetime:
    """Find the first local wall-clock time on an allowed weekday, strictly after ``after``."""
    if not days:
        raise ValueError("days must not be empty")
    day = after.astimezone(zone).date()
    for offset in range(8):
        candidate_day = day + timedelta(days=offset)
        if candidate_day.weekday() not in days:
            continue
        candidate = datetime.combine(candidate_day, t, tzinfo=zone).astimezone(timezone.utc)
        if candidate > after:
            return candidate
    raise AssertionError("unreachable")  # pragma: no cover


def _is_emoji(word: str) -> bool:
    return any(ord(c) > 127 for c in word) and not any(c.isalnum() for c in word)


def parse_ok_arg(arg: str) -> ConfirmRule | ParseError:
    """The rule after ``ok``: ``any``, choices, ``contains:<word>``, emoji or a word."""
    low = arg.casefold()
    if low == "any":
        return ConfirmRule("any")
    if "|" in arg:
        options = arg.split("|")
        if (
            not 2 <= len(options) <= 9
            or any(not any(c.isalnum() for c in option) for option in options)
            or len({normalize(option) for option in options}) != len(options)
        ):
            return ParseError(
                "`ok` choices need 2 to 9 non-empty, distinct options (ignoring case and punctuation).",
                "/snooze Dinner? at 8pm ok asian|french|italian",
            )
        return ConfirmRule("choice", arg)
    if low.startswith("contains:"):
        value = arg[len("contains:") :]
        if any(c.isalnum() for c in value):
            return ConfirmRule("contains", value)
        return ParseError(
            "`ok contains:` needs a word after the colon.", "/snooze Send the report at 4pm ok contains:sent"
        )
    if _is_emoji(arg):
        return ConfirmRule("emoji", arg)
    if any(c.isalnum() for c in arg):
        return ConfirmRule("word", arg)
    return ParseError(f"`ok {arg}` is not a word or an emoji.", EXAMPLE_IN)


@dataclass
class _Clauses:
    """The clauses read from the end of the text. `end` is how many words are left as the reminder text."""

    end: int
    at: time | None = None
    delay: timedelta | None = None  # `in <duration>`
    every: timedelta | None = None
    until: time | None = None
    confirm: ConfirmRule | None = None
    days: frozenset[int] | None = None  # weekly days: `daily`, `weekdays`, `every <days>`
    on_day: int | None = None  # `on <day>`: one time, on the next such day
    platform: str | None = None  # `via <platform>`
    start: str = ""  # the `at <time>` / `in <duration>` clause as written, for error messages


def _bad_length(what: str, duration: timedelta, example: str) -> ParseError | None:
    """An error when ``duration`` is under the minimum or over the maximum, else None."""
    if duration < MIN_DURATION:
        return ParseError(f"`{what}` is too short: the minimum is 1 minute.", example)
    if duration > MAX_DURATION:
        return ParseError(f"`{what}` is too long: the maximum is {MAX_DURATION.days} days.", example)
    return None


def _read_clauses(words: list[str], low: list[str]) -> _Clauses | ParseError:
    """Read ``at``, ``in``, ``every``, ``until``, ``on``, ``via`` and ``ok`` clauses from the end.

    ``every`` takes a duration (the interval) or weekday names (the weekly days); each can appear once.

    Stops at the first thing that is not a valid clause, or that repeats one already read.
    """
    c = _Clauses(end=len(words))
    while c.end > 0:
        if low[c.end - 1] in ("daily", "everyday", "weekdays"):
            if c.days is not None or c.on_day is not None:
                break
            c.days = frozenset(range(5)) if low[c.end - 1] == "weekdays" else frozenset(range(7))
            c.end -= 1
            continue
        if low[c.end - 1] == "ok":
            if c.confirm is not None:
                break
            c.confirm = ConfirmRule()
            c.end -= 1
            continue
        if c.end < 2:
            break
        keyword, arg = low[c.end - 2], words[c.end - 1]

        if keyword == "at":
            moment = _parse_time(arg)
            if moment is None or c.at is not None or c.delay is not None:
                break
            c.at = moment
            c.start = f"{words[c.end - 2]} {arg}"
        elif keyword == "until":
            moment = _parse_time(arg)
            if moment is None or c.until is not None:
                break
            c.until = moment
        elif keyword == "via":
            if low[c.end - 1] not in PLATFORMS:
                break  # `Go to Lyon via Dijon`: not a platform, so it stays in the text
            if c.platform is not None:
                return ParseError(
                    f"`via` twice: pick one platform, not `{c.platform}` and `{low[c.end - 1]}`.", EXAMPLE_VIA
                )
            c.platform = low[c.end - 1]
        elif keyword == "on":
            days = parse_days(arg)
            if days is None or c.days is not None or c.on_day is not None:
                break
            if len(days) > 1:
                return ParseError(f"`on {arg}` takes one day. For several days, use `every {arg}`.", EXAMPLE_WEEKLY)
            (c.on_day,) = days
        elif keyword in ("in", "every"):
            duration = parse_duration(arg)
            if duration is None and keyword == "every":
                days = parse_days(arg)
                if days is None or c.days is not None or c.on_day is not None:
                    break
                c.days = days
                c.end -= 2
                continue
            if duration is None:
                break
            if keyword == "in" and (c.delay is not None or c.at is not None):
                break
            if keyword == "every" and c.every is not None:
                break
            error = _bad_length(f"{keyword} {arg}", duration, EXAMPLE_LONG)
            if error:
                return error
            if keyword == "in":
                c.delay = duration
                c.start = f"{words[c.end - 2]} {arg}"
            else:
                c.every = duration
        elif keyword == "ok":
            if c.confirm is not None or low[c.end - 1] in _KEYWORDS:
                break
            rule = parse_ok_arg(arg)
            if isinstance(rule, ParseError):
                return rule
            c.confirm = rule
        else:
            break
        c.end -= 2
    return c


def _conflicting_start(c: _Clauses, words: list[str], low: list[str]) -> ParseError | None:
    """An ``at`` clause and an ``in`` clause together: the text left over ends with the other one."""
    if c.end < 2 or (c.at is None) == (c.delay is None):
        return None
    keyword, arg = low[c.end - 2], words[c.end - 1]
    other = (c.delay is not None and keyword == "at" and _parse_time(arg) is not None) or (
        c.at is not None and keyword == "in" and parse_duration(arg) is not None
    )
    if not other:
        return None
    return ParseError(
        f"Two start times: `{words[c.end - 2]} {arg}` and `{c.start}`. Use only one of `at` or `in`.",
        EXAMPLE_LONG if c.at is not None else EXAMPLE_IN,
    )


def _shown(parts: list[str], limit: int) -> str:
    return " ".join(parts[:limit]) + (" ..." if len(parts) > limit else "")


def _diagnose_tail(words: list[str], low: list[str], end: int) -> ParseError | None:
    """Explain why no start clause was read from ``words[:end]``, when a clause keyword shows the cause.

    Looks at the last ``at``, ``in``, ``every`` or ``until`` that has something after it. Returns None when
    the words give no sign of a clause, so the caller reports the generic missing start.
    """
    for j in range(end - 1, -1, -1):
        keyword = low[j]
        if keyword not in _TIME_CLAUSES and keyword not in _DURATION_CLAUSES:
            continue
        is_time = keyword in _TIME_CLAUSES
        example = EXAMPLE_IN if keyword == "in" else EXAMPLE_LONG
        tail = words[j + 1 : end]
        if not tail:
            return ParseError(f"`{words[j]}` needs a {'time' if is_time else 'duration'} after it.", example)
        arg = tail[0]
        valid = _parse_time(arg) is not None if is_time else parse_duration(arg) is not None
        if valid:
            if len(tail) > 1:
                return ParseError(
                    f"Unrecognized words after `{words[j]} {arg}`: `{_shown(tail[1:], 5)}`. "
                    "Clauses must come last; remove these words or move them before the clauses.",
                    example,
                )
            continue
        if is_time and any(ch.isdigit() for ch in arg):
            return ParseError(f"`{words[j]} {arg}` is not a valid time. Use a form like 4pm or 16:00.", example)
        if keyword == "every" and parse_days(arg) is not None:
            return ParseError(
                "Use `every <days>` once, and not together with `daily`, `weekdays` or `on <day>`.", EXAMPLE_WEEKLY
            )
        if keyword == "every" and arg.casefold() in ("day", "weekday"):
            return ParseError(
                f"`every {arg}` is not a clause. Use `daily` (or `everyday`) or `weekdays`.", EXAMPLE_RECURRING
            )
        if not is_time and (keyword == "every" or any(ch.isdigit() for ch in arg) or arg.casefold() in _SPOKEN_NUMBERS):
            return ParseError(
                f"`{words[j]} {_shown(tail, 3)}` is not a valid duration. "
                "Use digits with a unit, like 10m, 1h30m or 2 hours.",
                example,
            )
    return None


def _read_short_form(c: _Clauses, words: list[str], low: list[str]) -> ParseError | None:
    """Short forms, used when there is no ``at`` or ``in``: ``<what> 8am 10m`` and ``<what> 8am``."""

    def time_ending_at(i: int) -> time | None:
        """The time word just before position `i`, unless it follows a word like "before"."""
        if i < 2:  # keep at least one word of text
            return None
        if low[i - 2] in _PREPOSITIONS:
            return None
        return _parse_time(words[i - 1])

    last = words[c.end - 1]
    duration = parse_duration(last) if c.end >= 2 else None
    if c.every is None and duration is not None:
        moment = time_ending_at(c.end - 1)
        if moment is not None:
            error = _bad_length(last, duration, EXAMPLE_SHORT)
            if error:
                return error
            c.at, c.every, c.end = moment, duration, c.end - 2
            return None
    moment = time_ending_at(c.end)
    if moment is not None:
        c.at, c.end = moment, c.end - 1
    return None


def parse(text: str, now: datetime, tz: str | _tzinfo) -> Spec | ParseError:
    """Parse the ``/snooze`` argument text.

    ``now`` must be an aware datetime. ``tz`` is the user's zone (a ``tzinfo``
    or an IANA name such as ``"Europe/Paris"``). See the module docstring for
    the grammar and the shape of the result.
    """
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    zone = ZoneInfo(tz) if isinstance(tz, str) else tz
    tz_name = getattr(zone, "key", None) or str(zone)
    now_utc = now.astimezone(timezone.utc)

    toks = [(m.group(), m.start(), m.end()) for m in re.finditer(r"\S+", text or "")]
    if not toks:
        return ParseError("Nothing to remind.", EXAMPLE_SHORT)

    # `then` takes everything after it (a `then` as the very first word is plain text).
    then_text: str | None = None
    for k in range(1, len(toks)):
        if toks[k][0].casefold() == "then":
            then_text = text[toks[k][2] :].strip()
            if not then_text:
                return ParseError("`then` needs an action after it.", EXAMPLE_FULL)
            toks = toks[:k]
            break

    toks = _join_number_units(toks, text)
    words = [t[0] for t in toks]
    low = [w.casefold() for w in words]

    c = _read_clauses(words, low)
    if isinstance(c, ParseError):
        return c
    conflict = _conflicting_start(c, words, low)
    if conflict:
        return conflict
    if c.days is not None and c.delay is not None:
        return ParseError("A recurring snooze needs `at <time>`, not `in`.", EXAMPLE_RECURRING)
    if c.on_day is not None and c.delay is not None:
        return ParseError("`on <day>` needs `at <time>`, not `in`.", EXAMPLE_ON_DAY)
    if c.at is None and c.delay is None:
        error = _read_short_form(c, words, low)
        if error:
            return error

    what = text[: toks[c.end - 1][2]].strip() if c.end > 0 else ""
    if not what:
        return ParseError("Missing the reminder text.", EXAMPLE_LONG)
    if c.at is None and c.delay is None:
        return _diagnose_tail(words, low, c.end) or ParseError(
            "Missing when to start: add `at <time>` or `in <duration>`.", EXAMPLE_LONG
        )

    if c.delay is not None:
        first_at = now_utc + c.delay
    elif c.days is not None or c.on_day is not None:
        first_at = next_on_days(c.at, now_utc, zone, c.days or frozenset({c.on_day}))
    else:
        first_at = _next_occurrence(c.at, now_utc, zone)
    until = _next_occurrence(c.until, first_at, zone) if c.until is not None else None

    return Spec(
        text=what,
        first_at=first_at,
        every=c.every,
        until=until,
        confirm=c.confirm or ConfirmRule(),
        then=then_text,
        tz=tz_name,
        days=c.days,
        platform=c.platform,
    )
