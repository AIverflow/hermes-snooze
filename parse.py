"""Turn the text after ``/snooze`` into a ``Spec`` (or a ``ParseError``).

Pure module: standard library only, no Hermes imports. The grammar is
documented in the README ("Writing a snooze"). ``parse`` returns the error
instead of raising it.
"""

import re
from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
from datetime import tzinfo as _tzinfo
from zoneinfo import ZoneInfo

MIN_DURATION = timedelta(minutes=1)

_KEYWORDS = frozenset({"at", "in", "every", "until", "ok", "then"})
_PREPOSITIONS = frozenset({"before", "by", "after", "around", "since", "from", "to"})

_TIME_RE = re.compile(r"^(\d{1,2})(?::(\d{2}))?(am|pm)?$")
_DURATION_RE = re.compile(r"^(?:\d+(?:sec|min|hr|s|m|h))+$")
_DURATION_PART_RE = re.compile(r"(\d+)(sec|min|hr|s|m|h)")
_UNIT_SECONDS = {"s": 1, "sec": 1, "m": 60, "min": 60, "h": 3600, "hr": 3600}

EXAMPLE_FULL = "/snooze Send email to John Doe at 4pm every 10m until 6pm then close ticket 42"
EXAMPLE_SHORT = "/snooze Wake up 8am 10m"
EXAMPLE_LONG = "/snooze Wake up at 8am every 10m"
EXAMPLE_IN = "/snooze Take my pill in 30m every 5m ok taken"


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
    seconds = sum(int(n) * _UNIT_SECONDS[u] for n, u in _DURATION_PART_RE.findall(word))
    return timedelta(seconds=seconds)


def _next_occurrence(t: time, after: datetime, zone: _tzinfo) -> datetime:
    """First moment (UTC) at wall-clock time ``t`` in ``zone`` strictly after ``after``."""
    day = after.astimezone(zone).date()
    for _ in range(3):
        candidate = datetime.combine(day, t, tzinfo=zone).astimezone(timezone.utc)
        if candidate > after:
            return candidate
        day += timedelta(days=1)
    raise AssertionError("unreachable")  # pragma: no cover


def _is_emoji(word: str) -> bool:
    return any(ord(c) > 127 for c in word) and not any(c.isalnum() for c in word)


def parse_ok_arg(arg: str) -> ConfirmRule | ParseError:
    """The rule for the word after ``ok``: ``any``, ``contains:<word>``, an emoji or a word."""
    low = arg.casefold()
    if low == "any":
        return ConfirmRule("any")
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


def _too_short(what: str, example: str) -> ParseError:
    return ParseError(f"`{what}` is too short: the minimum is 1 minute.", example)


def _read_clauses(words: list[str], low: list[str]) -> _Clauses | ParseError:
    """Read ``at``, ``in``, ``every``, ``until`` and ``ok`` clauses from the end.

    Stops at the first thing that is not a valid clause, or that repeats one already read.
    """
    c = _Clauses(end=len(words))
    while c.end > 0:
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
        elif keyword == "until":
            moment = _parse_time(arg)
            if moment is None or c.until is not None:
                break
            c.until = moment
        elif keyword in ("in", "every"):
            duration = parse_duration(arg)
            if duration is None:
                break
            if keyword == "in" and (c.delay is not None or c.at is not None):
                break
            if keyword == "every" and c.every is not None:
                break
            if duration < MIN_DURATION:
                return _too_short(f"{keyword} {arg}", EXAMPLE_LONG)
            if keyword == "in":
                c.delay = duration
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
            if duration < MIN_DURATION:
                return _too_short(last, EXAMPLE_SHORT)
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

    words = [t[0] for t in toks]
    low = [w.casefold() for w in words]

    c = _read_clauses(words, low)
    if isinstance(c, ParseError):
        return c
    if c.at is None and c.delay is None:
        error = _read_short_form(c, words, low)
        if error:
            return error

    what = text[: toks[c.end - 1][2]].strip() if c.end > 0 else ""
    if not what:
        return ParseError("Missing the reminder text.", EXAMPLE_LONG)
    if c.at is None and c.delay is None:
        return ParseError("Missing when to start: add `at <time>` or `in <duration>`.", EXAMPLE_LONG)

    first_at = now_utc + c.delay if c.delay is not None else _next_occurrence(c.at, now_utc, zone)
    until = _next_occurrence(c.until, first_at, zone) if c.until is not None else None

    return Spec(
        text=what,
        first_at=first_at,
        every=c.every,
        until=until,
        confirm=c.confirm or ConfirmRule(),
        then=then_text,
        tz=tz_name,
    )
