"""Confirm matcher: decide what an inbound message means for the running snoozes.

Pure module: standard library plus the store's `Snooze` record.

    match(message, snoozes, sender, reply_to_id=None, is_reaction=False) -> Decision

`snoozes` are `store.Snooze` records; only those in state "active" or "pending"
count. Their `confirm` is the dict the store keeps: {"kind": "ok" | "word" |
"emoji" | "choice", "value": "<text>"}; `None`, an empty value or an unknown kind
(for example one from an older version) means the default (`ok`). `schedule.every` (minutes)
is the default `later` delay.

`sender` says who wrote the message. For a reaction, `message` is the emoji (a
string or a list of strings), `sender.user_id` is None (reactions carry none)
and `sender.chat_type` must say it is a private chat.

The matching rules (who can answer, exact answers, `later`, text folding) are
documented in `docs/user-guide.md` ("Confirmations").
"""

import re
import unicodedata
from dataclasses import dataclass
from typing import Any

from .store import ACTIVE, PENDING, Snooze

NONE = "none"
CONFIRM = "confirm"
LATER = "later"
YES = "yes"
INVALID = "invalid"  # not an answer, but Snooze owns the message: reply with the valid answers

DEFAULT_LATER_MINUTES = 10  # used only when the snooze has no usable interval

_VARIATION_SELECTORS = {"︎", "️"}
_SKIN_TONES = {chr(c) for c in range(0x1F3FB, 0x1F400)}
# Emoji variants that mean the same thing to a human, folded onto one symbol.
_EMOJI_CANON = {"✔": "✅", "☑": "✅", "✓": "✅"}

_PRIVATE_CHATS = {"dm", "private"}

_UNIT_MINUTES = {
    "m": 1,
    "min": 1,
    "mins": 1,
    "minute": 1,
    "minutes": 1,
    "h": 60,
    "hr": 60,
    "hrs": 60,
    "hour": 60,
    "hours": 60,
}
_DURATION = re.compile(r"^(\d+)([a-z]*)$")


@dataclass(frozen=True)
class Sender:
    """Who wrote the message (or put the reaction)."""

    platform: str
    chat_id: Any
    user_id: Any = None
    chat_type: str | None = None


@dataclass(frozen=True)
class Decision:
    """What to do with an inbound message."""

    action: str = NONE  # "none" | "confirm" | "later" | "yes" | "invalid"
    snooze_id: int | None = None  # the snooze concerned (None for "none")
    later_minutes: int | None = None  # only for "later"
    answer: str | None = None  # the option or message text that confirmed the occurrence


_NO_MATCH = Decision()


# ---------------------------------------------------------------- text


def normalize(text: Any) -> str:
    """Lower-case, strip punctuation / variation selectors / skin tones, fold emoji variants.

    Emoji (symbols) are set apart by spaces so "ok👍" reads as "ok 👍".
    """
    if text is None:
        return ""
    out: list[str] = []
    for ch in unicodedata.normalize("NFKC", str(text)):
        if ch in _VARIATION_SELECTORS or ch in _SKIN_TONES:
            continue
        cat = unicodedata.category(ch)
        if cat == "Cf":  # zero-width joiner, bidi marks...
            continue
        if cat.startswith("P") or cat.startswith("Z") or cat in ("Cc",):
            out.append(" ")
        elif cat == "So":
            out.append(" " + _EMOJI_CANON.get(ch, ch) + " ")
        else:
            out.append(_EMOJI_CANON.get(ch, ch).casefold())
    return " ".join("".join(out).split())


def _tokens(text: Any) -> list[str]:
    return normalize(text).split()


# ---------------------------------------------------------------- confirm rule


def _parse_rule(rule: dict | None) -> tuple[str, list[str] | list[list[str]]]:
    """Return (kind, value tokens) for a stored confirm rule; anything unusable is the default `ok`."""
    rule = rule or {}
    kind = str(rule.get("kind") or "ok").lower()
    tokens = _tokens(rule.get("value"))
    if kind == "choice":
        return "choice", [_tokens(option) for option in str(rule.get("value") or "").split("|")]
    if kind == "word" and tokens and not any(c.isalnum() for t in tokens for c in t):
        kind = "emoji"
    if kind not in ("word", "emoji") or not tokens:
        return "ok", ["ok"]
    return kind, tokens


def _confirm_match(rule: dict | None, tokens: list[str]) -> tuple[bool, str | None]:
    """Return (matched, answer): the whole message must be the answer."""
    kind, value = _parse_rule(rule)
    if kind == "choice":
        options = str((rule or {}).get("value") or "").split("|")
        for index, option_tokens in enumerate(value, start=1):
            if tokens == [str(index)] or (option_tokens and tokens == option_tokens):
                return True, options[index - 1]
        return False, None
    return tokens == value, None


def _is_emoji_rule(rule: dict | None) -> bool:
    return _parse_rule(rule)[0] == "emoji"


# ---------------------------------------------------------------- later / yes


def _later(tokens: list[str]):
    """If the whole message is a `later` / `not now` request, return (True, minutes or None)."""
    if tokens[:1] == ["later"]:
        rest = tokens[1:]
    elif tokens[:2] == ["not", "now"]:
        rest = tokens[2:]
    else:
        return False, None
    if not rest:
        return True, None
    m = _DURATION.match(rest[0])
    if not m:
        return False, None
    unit, extra = m.group(2), rest[1:]
    if not unit and extra and extra[0] in _UNIT_MINUTES:
        unit, extra = extra[0], extra[1:]
    if extra or (unit and unit not in _UNIT_MINUTES):
        return False, None
    minutes = int(m.group(1)) * _UNIT_MINUTES.get(unit, 1)
    return (True, minutes) if minutes > 0 else (False, None)


def _default_later(snooze: Snooze) -> int:
    every = snooze.schedule.every
    if isinstance(every, (int, float)) and not isinstance(every, bool) and every > 0:
        return max(1, round(every))
    return DEFAULT_LATER_MINUTES


# ---------------------------------------------------------------- who / which


def _same(a: Any, b: Any) -> bool:
    return a is not None and b is not None and str(a) == str(b)


def _authorized(snooze: Snooze, sender: Sender, is_reaction: bool) -> bool:
    target = snooze.target
    if not _same(target.platform.lower(), str(sender.platform).lower()):
        return False
    if not _same(target.chat_id, sender.chat_id):
        return False
    if is_reaction:
        # Reactions carry no reliable user id: private chats only, and a user id,
        # when present, must still match.
        if str(sender.chat_type or "").lower() not in _PRIVATE_CHATS:
            return False
        return sender.user_id is None or _same(target.user_id, sender.user_id)
    return _same(target.user_id, sender.user_id)


def _last_sent(snoozes: list[Snooze]) -> Snooze | None:
    """The snooze that sent a reminder last (ISO timestamps sort as text); later ones win ties."""
    return max(
        (s for s in reversed(snoozes) if s.last_sent_at is not None),
        key=lambda s: s.last_sent_at,
        default=None,
    )


# ---------------------------------------------------------------- entry point


def match(
    message: str | list[str],
    snoozes: list[Snooze],
    sender: Sender,
    reply_to_id: Any = None,
    is_reaction: bool = False,
) -> Decision:
    """Decide what `message` means. See the module docstring for the inputs."""
    if is_reaction and not isinstance(message, str):
        message = " ".join(message)
    tokens = _tokens(message)
    if not tokens:
        return _NO_MATCH

    mine = [s for s in snoozes if s.state in (ACTIVE, PENDING) and _authorized(s, sender, is_reaction)]

    replied = None
    if reply_to_id is not None and str(reply_to_id) != "":
        hits = [s for s in mine if _same(s.last_message_id, reply_to_id)]
        if hits:
            replied = hits[-1]
        elif is_reaction:
            return _NO_MATCH  # a reaction counts only on a reminder

    if replied is not None:
        active = replied if replied.state == ACTIVE else None
        pending = replied if replied.state == PENDING else None
    else:
        active = _last_sent([s for s in mine if s.state == ACTIVE])
        pendings = [s for s in mine if s.state == PENDING]
        pending = pendings[-1] if pendings else None

    if is_reaction:
        if active is not None and _is_emoji_rule(active.confirm) and _confirm_match(active.confirm, tokens)[0]:
            return Decision(CONFIRM, active.id)
        return _NO_MATCH

    if active is not None:
        is_later, minutes = _later(tokens)
        if is_later:
            return Decision(LATER, active.id, minutes if minutes is not None else _default_later(active))
        matched, answer = _confirm_match(active.confirm, tokens)
        if matched:
            return Decision(CONFIRM, active.id, answer=answer)

    if pending is not None and tokens == ["yes"]:
        return Decision(YES, pending.id)

    # Not an answer. Snooze still owns a reply to a reminder, and the first message after one.
    if active is not None and (replied is not None or active.awaiting_answer):
        return Decision(INVALID, active.id)

    return _NO_MATCH
