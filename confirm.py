"""Confirm matcher: decide what an inbound message means for the running snoozes.

Pure module: standard library plus the store's `Snooze` record.

    match(message, snoozes, sender, reply_to_id=None, is_reaction=False) -> Decision

`snoozes` are `store.Snooze` records; only those in state "active" or "pending"
count. Their `confirm` is the dict the store keeps: {"kind": "ok" | "word" |
"contains" | "any" | "emoji", "value": "<text>"}; `None` or an empty value means
the default (`ok`). `schedule.every` (minutes) is the default `later` delay.

`sender` says who wrote the message. For a reaction, `message` is the emoji (a
string or a list of strings), `sender.user_id` is None (reactions carry none)
and `sender.chat_type` must say it is a private chat.

The matching rules (who can answer, `later`, pass-through, text folding) are
documented in the README ("Confirming").
"""

import re
import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from .store import ACTIVE, PENDING, Snooze

NONE = "none"
CONFIRM = "confirm"
LATER = "later"
YES = "yes"

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

    action: str = NONE  # "none" | "confirm" | "later" | "yes"
    snooze_id: int | None = None  # the snooze concerned (None for "none")
    later_minutes: int | None = None  # only for "later"
    pass_through: bool = True  # True: let the message reach the agent


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


def _has_words(tokens: Iterable[str]) -> bool:
    """True if some token carries letters or digits (not just emoji / symbols)."""
    return any(any(c.isalnum() for c in t) for t in tokens)


def _find(tokens: list[str], needle: list[str]) -> int:
    """Index of the first occurrence of `needle` in `tokens`, or -1."""
    n = len(needle)
    if n == 0:
        return -1
    for i in range(len(tokens) - n + 1):
        if tokens[i : i + n] == needle:
            return i
    return -1


# ---------------------------------------------------------------- confirm rule


def _parse_rule(rule: dict | None) -> tuple[str, list[str]]:
    """Return (kind, value tokens) for a stored confirm rule."""
    rule = rule or {}
    kind = str(rule.get("kind") or "ok").lower()
    tokens = _tokens(rule.get("value"))
    if kind == "ok":
        return "ok", ["ok"]
    if kind == "any":
        return "any", []
    if kind == "word" and tokens and not _has_words(tokens):
        kind = "emoji"
    if kind in ("word", "emoji", "contains") and not tokens:
        return "ok", ["ok"]  # empty value: fall back to the default
    return kind, tokens


def _confirm_match(rule: dict | None, tokens: list[str]):
    """Return (matched, pass_through) for a message against a rule."""
    kind, value = _parse_rule(rule)
    if not tokens:
        return False, False
    if kind == "any":
        return True, False
    if kind == "contains":
        return _find(tokens, value) >= 0, False
    if kind in ("ok", "word"):
        if tokens[: len(value)] != value:
            return False, False
        return True, _has_words(tokens[len(value) :])
    if kind == "emoji":
        i = _find(tokens, value)
        if i < 0:
            return False, False
        rest = tokens[:i] + tokens[i + len(value) :]
        return True, _has_words(rest)
    return False, False


def _is_emoji_rule(rule: dict | None) -> bool:
    return _parse_rule(rule)[0] == "emoji"


# ---------------------------------------------------------------- later / yes


def _later(tokens: list[str]):
    """If the message is a `later` / `not now` request, return (True, minutes or None)."""
    if tokens[:1] == ["later"]:
        rest = tokens[1:]
    elif tokens[:2] == ["not", "now"]:
        rest = tokens[2:]
    else:
        return False, None
    if rest:
        m = _DURATION.match(rest[0])
        if m:
            unit = m.group(2)
            if not unit and len(rest) > 1 and rest[1] in _UNIT_MINUTES:
                unit = rest[1]
            if unit == "" or unit in _UNIT_MINUTES:
                minutes = int(m.group(1)) * _UNIT_MINUTES.get(unit, 1)
                if minutes > 0:
                    return True, minutes
    return True, None


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
    sent = [(s.last_sent_at, i, s) for i, s in enumerate(snoozes) if s.last_sent_at is not None]
    return max(sent, key=lambda item: item[:2])[2] if sent else None


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
        replied = hits[-1] if hits else None

    if replied is not None:
        active = replied if replied.state == ACTIVE else None
        pending = replied if replied.state == PENDING else None
    else:
        active = _last_sent([s for s in mine if s.state == ACTIVE])
        pendings = [s for s in mine if s.state == PENDING]
        pending = pendings[-1] if pendings else None

    if is_reaction:
        if active is not None and _is_emoji_rule(active.confirm) and _confirm_match(active.confirm, tokens)[0]:
            return Decision(CONFIRM, active.id, None, False)
        return _NO_MATCH

    if active is not None:
        is_later, minutes = _later(tokens)
        if is_later:
            return Decision(LATER, active.id, minutes if minutes is not None else _default_later(active), False)
        matched, extra = _confirm_match(active.confirm, tokens)
        if matched:
            return Decision(CONFIRM, active.id, None, extra)

    if pending is not None and tokens == ["yes"]:
        return Decision(YES, pending.id, None, False)

    return _NO_MATCH
