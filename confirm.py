"""Confirm matcher: decide what an inbound message means for the active snoozes.

Pure module: standard library only, no Hermes, no store/parse imports.

    match(message, active_snoozes, sender, reply_to_id, is_reaction=False) -> Decision

Expected input shapes (every "object" may be a dict or any object with attributes):

snooze (one item of `active_snoozes`)
    id               snooze id (any hashable; compared as given)
    state            "active" (reminders are being sent) or "pending" (waiting for
                     the user to validate the setup with `yes`); other states are ignored
    confirm          the confirm rule, either a string or a dict
                       "ok"                 default: the message is `ok`
                       "any"                any message
                       "contains:<word>"    the word anywhere in the message
                       "<word>"             the message is that word (or starts with it)
                       "<emoji>"            that emoji, in a message or as a reaction
                       {"kind": "ok"|"any"|"contains"|"word"|"emoji", "value": "<text>"}
                     `None` or an empty value means the default (`ok`).
    target           where the reminder went: platform, chat_id, user_id
    last_sent_at     when the last reminder was sent (None if never); any orderable value
                     (ISO string, number, datetime)
    last_message_id  id of the last reminder message in the chat (None if unknown)
    schedule.every   repeat interval, in minutes (number) or a datetime.timedelta;
                     used as the default `later` delay

sender
    platform, chat_id, user_id   who wrote the message
    chat_type                    optional; "dm" or "private" marks a private chat
                                 (only used for reactions)

Rules
    * Only the exact chat + user the reminder went to can answer (platform, chat id and
      user id must all match; a missing user id never matches).
    * Reactions carry no user id. With `is_reaction=True` the message is the emoji (a string
      or a list of strings); it is accepted only in a private chat whose chat id matches,
      and only for an emoji rule.
    * Which snooze: a reply to a reminder message picks that snooze; otherwise the active
      snooze whose reminder was sent last.
    * `later`, `later 30`, `later 1h`, `not now` always win over the confirm rule. They are
      always consumed (pass_through False). Without a number the delay is the snooze interval.
    * Pure confirm -> consumed. For the `ok`, word and emoji rules a confirm with more text
      ("ok, and book a table") confirms AND passes the message on to the agent. For the `any`
      and `contains` rules the whole message is the answer, so it is consumed.
    * `yes` (the whole message) validates a snooze in state `pending`.
    * Text is compared ignoring case, spaces, punctuation, emoji variation selectors, skin tones
      and emoji variants (check marks are all the same).
"""
import datetime
import re
import unicodedata
from dataclasses import dataclass
from typing import Any, Iterable, List, Optional

NONE = "none"
CONFIRM = "confirm"
LATER = "later"
YES = "yes"

DEFAULT_LATER_MINUTES = 10  # used only when the snooze has no usable interval

_VARIATION_SELECTORS = {"︎", "️"}
_SKIN_TONES = {chr(c) for c in range(0x1F3FB, 0x1F400)}
# Emoji variants that mean the same thing to a human, folded onto one symbol.
_EMOJI_CANON = {"✔": "✅", "☑": "✅", "✓": "✅"}

_PENDING = "pending"
_ACTIVE = "active"
_PRIVATE_CHATS = {"dm", "private"}

_UNIT_MINUTES = {
    "m": 1, "min": 1, "mins": 1, "minute": 1, "minutes": 1,
    "h": 60, "hr": 60, "hrs": 60, "hour": 60, "hours": 60,
}
_DURATION = re.compile(r"^(\d+)([a-z]*)$")


@dataclass(frozen=True)
class Decision:
    """What to do with an inbound message."""
    action: str = NONE                  # "none" | "confirm" | "later" | "yes"
    snooze_id: Any = None               # the snooze concerned (None for "none")
    later_minutes: Optional[int] = None  # only for "later"
    pass_through: bool = True           # True: let the message reach the agent


_NO_MATCH = Decision()


def _get(obj: Any, name: str, default: Any = None) -> Any:
    if obj is None:
        return default
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


# ---------------------------------------------------------------- text

def normalize(text: Any) -> str:
    """Lower-case, strip punctuation / variation selectors / skin tones, fold emoji variants.

    Emoji (symbols) are set apart by spaces so "ok👍" reads as "ok 👍".
    """
    if text is None:
        return ""
    out: List[str] = []
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


def _tokens(text: Any) -> List[str]:
    return normalize(text).split()


def _has_words(tokens: Iterable[str]) -> bool:
    """True if some token carries letters or digits (not just emoji / symbols)."""
    return any(any(c.isalnum() for c in t) for t in tokens)


def _find(tokens: List[str], needle: List[str]) -> int:
    """Index of the first occurrence of `needle` in `tokens`, or -1."""
    n = len(needle)
    if n == 0:
        return -1
    for i in range(len(tokens) - n + 1):
        if tokens[i:i + n] == needle:
            return i
    return -1


# ---------------------------------------------------------------- confirm rule

def _parse_rule(rule: Any):
    """Return (kind, value tokens) for a confirm rule."""
    kind, value = None, ""
    if isinstance(rule, dict) or (rule is not None and not isinstance(rule, str)):
        kind = _get(rule, "kind")
        value = _get(rule, "value") or ""
        kind = str(kind).lower() if kind else None
    elif isinstance(rule, str):
        raw = rule.strip()
        low = raw.lower()
        if low.startswith("contains:"):
            kind, value = "contains", raw[len("contains:"):]
        elif low in ("", "ok"):
            kind = "ok"
        elif low == "any":
            kind = "any"
        else:
            kind, value = "word", raw
    if kind in (None, ""):
        kind = "ok"
    toks = _tokens(value)
    if kind == "ok":
        return "ok", ["ok"]
    if kind == "any":
        return "any", []
    if kind == "word" and toks and not _has_words(toks):
        kind = "emoji"
    if kind in ("word", "emoji", "contains") and not toks:
        return "ok", ["ok"]  # empty value: fall back to the default
    return kind, toks


def _confirm_match(rule: Any, tokens: List[str]):
    """Return (matched, pass_through) for a message against a rule."""
    kind, value = _parse_rule(rule)
    if not tokens:
        return False, False
    if kind == "any":
        return True, False
    if kind == "contains":
        return _find(tokens, value) >= 0, False
    if kind in ("ok", "word"):
        if tokens[:len(value)] != value:
            return False, False
        return True, _has_words(tokens[len(value):])
    if kind == "emoji":
        i = _find(tokens, value)
        if i < 0:
            return False, False
        rest = tokens[:i] + tokens[i + len(value):]
        return True, _has_words(rest)
    return False, False


def _is_emoji_rule(rule: Any) -> bool:
    return _parse_rule(rule)[0] == "emoji"


# ---------------------------------------------------------------- later / yes

def _later(tokens: List[str]):
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


def _default_later(snooze: Any) -> int:
    every = _get(_get(snooze, "schedule"), "every")
    if isinstance(every, datetime.timedelta):
        every = every.total_seconds() / 60
    if isinstance(every, (int, float)) and not isinstance(every, bool) and every > 0:
        return max(1, int(round(every)))
    return DEFAULT_LATER_MINUTES


# ---------------------------------------------------------------- who / which

def _same(a: Any, b: Any) -> bool:
    return a is not None and b is not None and str(a) == str(b)


def _authorized(snooze: Any, sender: Any, is_reaction: bool) -> bool:
    target = _get(snooze, "target")
    if target is None or sender is None:
        return False
    t_platform, s_platform = _get(target, "platform"), _get(sender, "platform")
    if not _same(str(t_platform).lower() if t_platform is not None else None,
                 str(s_platform).lower() if s_platform is not None else None):
        return False
    if not _same(_get(target, "chat_id"), _get(sender, "chat_id")):
        return False
    if is_reaction:
        # Reactions carry no (reliable) user id: private chats only, and a user id,
        # when present, must still match.
        if str(_get(sender, "chat_type") or "").lower() not in _PRIVATE_CHATS:
            return False
        s_user = _get(sender, "user_id")
        return s_user is None or _same(_get(target, "user_id"), s_user)
    return _same(_get(target, "user_id"), _get(sender, "user_id"))


def _last_sent(snoozes: List[Any]) -> Optional[Any]:
    sent = [(i, s) for i, s in enumerate(snoozes) if _get(s, "last_sent_at") is not None]
    if not sent:
        return None
    try:
        return max(sent, key=lambda p: (_get(p[1], "last_sent_at"), p[0]))[1]
    except TypeError:  # values of different types: trust the list order
        return sent[-1][1]


def _state(snooze: Any) -> str:
    return str(_get(snooze, "state") or "").lower()


# ---------------------------------------------------------------- entry point

def match(message: Any, active_snoozes: Iterable[Any], sender: Any,
          reply_to_id: Any = None, is_reaction: bool = False) -> Decision:
    """Decide what `message` means. See the module docstring for the input shapes."""
    if is_reaction and not isinstance(message, str):
        message = " ".join(str(m) for m in (message or ()))
    tokens = _tokens(message)
    if not tokens:
        return _NO_MATCH

    mine = [s for s in (active_snoozes or ()) if _authorized(s, sender, is_reaction)]
    mine = [s for s in mine if _state(s) in (_ACTIVE, _PENDING)]
    if not mine:
        return _NO_MATCH

    replied = None
    if reply_to_id is not None and str(reply_to_id) != "":
        hits = [s for s in mine if _same(_get(s, "last_message_id"), reply_to_id)]
        replied = hits[-1] if hits else None

    if replied is not None:
        active = replied if _state(replied) == _ACTIVE else None
        pending = replied if _state(replied) == _PENDING else None
    else:
        active = _last_sent([s for s in mine if _state(s) == _ACTIVE])
        pendings = [s for s in mine if _state(s) == _PENDING]
        pending = pendings[-1] if pendings else None

    if is_reaction:
        if active is not None and _is_emoji_rule(_get(active, "confirm")):
            ok, _ = _confirm_match(_get(active, "confirm"), tokens)
            if ok:
                return Decision(CONFIRM, _get(active, "id"), None, False)
        return _NO_MATCH

    if active is not None:
        is_later, minutes = _later(tokens)
        if is_later:
            return Decision(LATER, _get(active, "id"),
                            minutes if minutes is not None else _default_later(active), False)
        ok, extra = _confirm_match(_get(active, "confirm"), tokens)
        if ok:
            return Decision(CONFIRM, _get(active, "id"), None, extra)

    if pending is not None and tokens == ["yes"]:
        return Decision(YES, _get(pending, "id"), None, False)

    return _NO_MATCH
