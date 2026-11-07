"""Turn the text after ``/snooze`` into a ``Spec`` (or a ``ParseError``).

Pure module: standard library only, no Hermes imports.

Grammar (keywords are case-insensitive, any order after the text)::

    <what> [at <time> | in <duration>] [every <duration>] [until <time>]
           [ok <word>] [then <action>]

Rules:

* ``then <action>`` takes everything after the first ``then`` (a ``then`` as
  the very first word is plain text).
* The words before ``then`` are read from the end. Each ``at``, ``in``,
  ``every``, ``until`` or ``ok`` clause is cut off while it is valid and not a
  repeat of one already read. Reading stops at the first thing that is not a
  clause, so anything else stays in the text ("before 6pm" is text).
* Short form: ``<what> 8am 10m`` means ``at 8am every 10m``. A lone trailing
  time (``<what> 8am``) means ``at 8am``. A time right after a word like
  ``before``/``by`` is not taken as a short-form time.
* Times: ``8am``, ``8:30am``, ``4pm``, ``16:00``. A time that already passed
  today means tomorrow (calendar day in the user's zone, so DST days are right;
  a time skipped by a DST jump moves forward, a repeated one uses its first
  occurrence).
* ``until <time>`` is the first such time strictly after the first reminder.
* Durations: ``10m``, ``1h``, ``90s``, ``1h30m`` (units ``s m h``, also ``sec
  min hr``). Minimum 1 minute, for ``in`` and ``every``.
* ``ok`` forms: ``ok`` (default), ``ok <word>``, ``ok contains:<word>``,
  ``ok any``, ``ok <emoji>``.

Result::

    Spec
        text      str              what to remind (never empty)
        first_at  datetime         first reminder, aware, in UTC
        every     timedelta|None   repeat interval; None = use the plugin default
        until     datetime|None    stop time, aware, in UTC; None = no explicit stop
        confirm   ConfirmRule      how the user confirms (see below)
        then      str|None         action text for the agent after the confirm
        tz        str              the user's time zone name, for local-time echoes

    ConfirmRule
        kind      "ok" | "word" | "contains" | "any" | "emoji"
        value     str              the word / substring / emoji; "" for ok and any
                                   (matching rules such as case are confirm.py's job)

    ParseError
        message   str              what is wrong, one short sentence
        example   str              a working example line for the echo

``parse`` returns the error instead of raising it.
"""
import re
from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone, tzinfo as _tzinfo
from typing import Optional, Union
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
    every: Optional[timedelta]
    until: Optional[datetime]
    confirm: ConfirmRule
    then: Optional[str]
    tz: str


@dataclass(frozen=True)
class ParseError:
    message: str
    example: str

    def __str__(self) -> str:
        return f"{self.message} Example: {self.example}"


def _parse_time(word: str) -> Optional[time]:
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


def _parse_duration(word: str) -> Optional[timedelta]:
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


def _parse_ok_arg(arg: str) -> Union[ConfirmRule, ParseError]:
    low = arg.casefold()
    if low == "any":
        return ConfirmRule("any")
    if low.startswith("contains:"):
        value = arg[len("contains:"):]
        if any(c.isalnum() for c in value):
            return ConfirmRule("contains", value)
        return ParseError("`ok contains:` needs a word after the colon.",
                          "/snooze Send the report at 4pm ok contains:sent")
    if _is_emoji(arg):
        return ConfirmRule("emoji", arg)
    if any(c.isalnum() for c in arg):
        return ConfirmRule("word", arg)
    return ParseError(f"`ok {arg}` is not a word or an emoji.", EXAMPLE_IN)


def parse(text: str, now: datetime, tz: Union[str, _tzinfo]) -> Union[Spec, ParseError]:
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

    # `then` takes everything after it.
    then_text: Optional[str] = None
    for k in range(1, len(toks)):
        if toks[k][0].casefold() == "then":
            then_text = text[toks[k][2]:].strip()
            if not then_text:
                return ParseError("`then` needs an action after it.", EXAMPLE_FULL)
            toks = toks[:k]
            break

    words = [t[0] for t in toks]
    low = [w.casefold() for w in words]

    at_t: Optional[time] = None
    in_d: Optional[timedelta] = None
    every: Optional[timedelta] = None
    until_t: Optional[time] = None
    confirm: Optional[ConfirmRule] = None

    # Read clauses from the end; stop at the first one that does not apply.
    end = len(toks)
    while end > 0:
        if low[end - 1] == "ok":
            if confirm is not None:
                break
            confirm = ConfirmRule()
            end -= 1
            continue
        if end < 2:
            break
        kw, arg = low[end - 2], words[end - 1]
        if kw in ("at", "until"):
            t = _parse_time(arg)
            if t is None:
                break
            if kw == "at":
                if at_t is not None or in_d is not None:
                    break
                at_t = t
            else:
                if until_t is not None:
                    break
                until_t = t
        elif kw in ("in", "every"):
            d = _parse_duration(arg)
            if d is None:
                break
            if kw == "in":
                if in_d is not None or at_t is not None:
                    break
            elif every is not None:
                break
            if d < MIN_DURATION:
                return ParseError(f"`{kw} {arg}` is too short: the minimum is 1 minute.",
                                  EXAMPLE_LONG)
            if kw == "in":
                in_d = d
            else:
                every = d
        elif kw == "ok":
            if confirm is not None or low[end - 1] in _KEYWORDS:
                break
            rule = _parse_ok_arg(arg)
            if isinstance(rule, ParseError):
                return rule
            confirm = rule
        else:
            break
        end -= 2

    # Short form: `<what> 8am 10m` or `<what> 8am`.
    if at_t is None and in_d is None:
        def bare_time_at(i: int) -> Optional[time]:
            if i < 2:  # keep at least one word of text
                return None
            if i >= 2 and low[i - 2] in _PREPOSITIONS:
                return None
            return _parse_time(words[i - 1])

        if every is None and end >= 2 and _parse_duration(words[end - 1]) is not None:
            t = bare_time_at(end - 1)
            if t is not None:
                d = _parse_duration(words[end - 1])
                if d < MIN_DURATION:
                    return ParseError(f"`{words[end - 1]}` is too short: the minimum is 1 minute.",
                                      EXAMPLE_SHORT)
                at_t, every, end = t, d, end - 2
        if at_t is None:
            t = bare_time_at(end)
            if t is not None:
                at_t, end = t, end - 1

    what = text[:toks[end - 1][2]].strip() if end > 0 else ""
    if not what:
        return ParseError("Missing the reminder text.", EXAMPLE_LONG)
    if at_t is None and in_d is None:
        return ParseError("Missing when to start: add `at <time>` or `in <duration>`.",
                          EXAMPLE_LONG)

    if in_d is not None:
        first_at = now_utc + in_d
    else:
        first_at = _next_occurrence(at_t, now_utc, zone)
    until = _next_occurrence(until_t, first_at, zone) if until_t is not None else None

    return Spec(
        text=what,
        first_at=first_at,
        every=every,
        until=until,
        confirm=confirm or ConfirmRule(),
        then=then_text,
        tz=tz_name,
    )
