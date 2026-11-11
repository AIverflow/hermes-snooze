"""The `/snooze` slash command and the `snooze_create` tool.

Hermes glue: nothing from Hermes is imported at the top of this file. Hermes
modules are imported inside the small helpers that need them, so the module
loads (and the tests run) without Hermes, using a fake `ctx`. The commands and
settings are documented in `docs/user-guide.md` ("Setup and configuration").

Stored shapes (see `store.py`): `schedule.every` is the repeat interval in
MINUTES (a number, which is what `confirm.py` reads); `confirm` is a dict
{"kind": ..., "value": ...}.

A pending snooze that gets no `yes` within `PENDING_TIMEOUT` is not started
(`pending_deadline` / `is_pending_expired`). The sweep belongs to the timer;
`yes` refuses an expired snooze and marks it "missed". The reminders cap
(`cap_reminders`) is enforced where reminders are sent.
"""

from __future__ import annotations

import dataclasses
import json
import os
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone, tzinfo
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from . import echo
from . import parse as _parse
from .echo import WHERE_CHAT, WHERE_HOME
from .parse import ConfirmRule, ParseError, Spec
from .schedule import PENDING_TIMEOUT, apply_later, is_pending_expired, is_recurring, pending_deadline, rearm
from .store import ACTIVE, CANCELLED, DONE, MISSED, PENDING, Schedule, Snooze, Store, StoreError, Target, Txn

PLUGIN_NAME = "snooze"
TOOL_NAME = "snooze_create"

THEN_NOT_RUN_TEXT = "⚠️ The then-action was not run: it runs only when you confirm from the chat."

DEFAULT_EVERY = timedelta(minutes=10)  # used when the text gives no `every`

DEFAULT_VALIDATE_SETUP = False

DEFAULT_CAP_MINUTES = 120
DEFAULT_CAP_REMINDERS = 10

# Session platforms that are not a chat the reminder could be sent to.
NON_CHAT_PLATFORMS = frozenset(
    {
        "",
        "api_server",
        "cli",
        "codex",
        "desktop",
        "gateway",
        "kanban",
        "local",
        "msgraph_webhook",
        "tool",
        "tui",
        "webhook",
        "acp",
    }
)

SUBCOMMANDS = ("list", "ok", "later", "cancel", "yes", "test", "platform")

USAGE = {
    "list": "Usage: /snooze list",
    "ok": "Usage: /snooze ok [id]",
    "later": "Usage: /snooze later [minutes] [id]   (for example: /snooze later 30)",
    "cancel": "Usage: /snooze cancel <id>",
    "yes": "Usage: /snooze yes [id]",
    "test": "Usage: /snooze test",
    "platform": "Usage: /snooze platform [telegram | discord | source]",
}

# The three levels of a snooze. The help below and `hermes snooze add --help` show them;
# the tests parse them, so they cannot go stale.
EXAMPLE_SIMPLE = "/snooze Drink water in 20m every 10m"
EXAMPLE_ACTION = "/snooze Stop the GPU at 19:00 every 10m then shut it down"
EXAMPLE_CHOICE = "/snooze Merge? in 30m every 10m ok merge|review then do what I picked"

HELP = "\n".join(
    [
        "/snooze <what> (at <time> | in <duration>) [every <duration>] [ok <answer>] [then <action>]",
        f"1. Simple:  {EXAMPLE_SIMPLE}   → reply: Ok | later",
        f"2. Action:  {EXAMPLE_ACTION}   → reply: Ok | later, then the agent acts",
        f"3. Choice:  {EXAMPLE_CHOICE}   → reply: merge | review | later (1 | 2 also work)",
        "Other commands: list | ok [id] | later [minutes] [id] | cancel <id> | yes [id] | test | platform [name]",
        "More keywords and examples: see the user guide (docs/user-guide.md).",
    ]
)

PAUSED_TEXT = "⏸ Hermes is paused: no reminders until hermes resume"

NO_TARGET_TEXT = (
    "No chat to send reminders to: run this from a chat (Telegram, Discord, ...), "
    "or set a home channel for the profile. Nothing was set."
)

# Where a command came from, for the texts that name the command to type next.
FROM_CHAT, FROM_TERMINAL, FROM_TOOL = "chat", "terminal", "tool"

OUT_OF_RANGE_TEXT = "⚠️ A date or number is out of range. Nothing was set."


# -- settings ----------------------------------------------------------------


@dataclass(frozen=True)
class Settings:
    validate_setup: bool = DEFAULT_VALIDATE_SETUP
    cap_minutes: int = DEFAULT_CAP_MINUTES
    cap_reminders: int = DEFAULT_CAP_REMINDERS
    timezone: str = "UTC"  # an IANA name; "system" is already resolved
    notification_platform: str = "source"


def _is_positive_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def read_settings(ctx: Any) -> Settings:
    """Read the plugin config through `ctx.get_config(key, default)`. An invalid value falls back to its default."""

    def get(key: str, default: Any, is_valid: Callable[[Any], bool]) -> Any:
        value = ctx.get_config(key, default)
        return value if is_valid(value) else default

    notification_platform = ctx.get_config("notification_platform", "source")
    if not isinstance(notification_platform, str) or notification_platform.lower() not in {
        "source",
        *_parse.PLATFORMS,
    }:
        notification_platform = "source"

    return Settings(
        validate_setup=get("validate_setup", DEFAULT_VALIDATE_SETUP, lambda v: isinstance(v, bool)),
        cap_minutes=get("cap_minutes", DEFAULT_CAP_MINUTES, _is_positive_int),
        cap_reminders=get("cap_reminders", DEFAULT_CAP_REMINDERS, _is_positive_int),
        timezone=resolve_timezone(ctx.get_config("timezone", "system")),
        notification_platform=notification_platform.lower(),
    )


def _valid_zone(name: str) -> bool:
    try:
        ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, OSError):
        return False
    return True


def system_timezone() -> str:
    """IANA name of the machine's time zone (from TZ, else the /etc/localtime link), or "UTC"."""
    candidates = [os.environ.get("TZ", "").lstrip(":")]
    real = os.path.realpath("/etc/localtime")
    if "zoneinfo/" in real:
        candidates.append(real.split("zoneinfo/", 1)[1])
    return next((name for name in candidates if name and _valid_zone(name)), "UTC")


def resolve_timezone(value: Any) -> str:
    """The configured zone name, or the machine's zone for "system", empty or unknown names."""
    name = str(value or "").strip()
    if name and name.lower() != "system" and _valid_zone(name):
        return name
    return system_timezone()


def not_started_message(snooze: Snooze) -> str:
    return f"⌛ Not started: {snooze.text}"


# -- state changes shared with the gateway hooks (hooks.py) ---------------------
# Each takes a record inside a store transaction, changes it, and returns the reply text.


def confirm_snooze(snooze: Snooze, now: datetime, answer: str | None = None) -> str:
    """The user confirmed a running snooze: finish its occurrence or re-arm its series."""
    snooze.answer = answer
    answer_text = f" → {answer}" if answer and echo.choice_options(snooze.confirm) else ""
    if is_recurring(snooze):
        next_at = rearm(snooze, now)
        when = echo.format_when(next_at, snooze.schedule.tz, now)
        if snooze.then:
            return f"✅ Done for today: {snooze.text}{answer_text}. Now: {snooze.then}… Next: {when}"
        return f"✅ Done for today: {snooze.text}{answer_text}. Next: {when}"
    snooze.state, snooze.next_at = DONE, None
    if snooze.test:
        return echo.TEST_PASSED
    if snooze.then:
        return f"✅ Done: {snooze.text}{answer_text}. Now: {snooze.then}…"
    return f"✅ Done: {snooze.text}{answer_text}"


def push_back_snooze(snooze: Snooze, now: datetime, minutes: float, cap_reminders: int) -> str:
    """The user asked for the next reminder in `minutes`. Refused, with the snooze untouched, at the reminder cap."""
    at = apply_later(snooze, now, minutes, cap_reminders)
    if at is None:
        return (
            f"⏰ Can't push back #{snooze.id}: it already sent {cap_reminders} reminders, the maximum "
            f"(cap_reminders). Reply Ok to answer it."
        )
    return f"⏰ OK, back at {echo.format_when(at, snooze.schedule.tz, now, relative_to=now)}: {snooze.text}"


def start_snooze(snooze: Snooze, now: datetime) -> str:
    """The user said `yes` to a pending snooze: it starts, unless its `yes` window has closed."""
    if is_pending_expired(snooze, now):
        snooze.state, snooze.next_at = MISSED, None
        return not_started_message(snooze)
    snooze.state, snooze.next_at = ACTIVE, snooze.schedule.first_at
    first = echo.from_iso(snooze.schedule.first_at)
    when = f" First reminder: {echo.format_when(first, snooze.schedule.tz, now)}." if first else ""
    return f"▶️ Started #{snooze.id}: {snooze.text}.{when}"


# -- small helpers -------------------------------------------------------------


def _no_media_tag(text: str | None) -> str | None:
    """Hermes sends a `MEDIA:` tag in a reply as a local file: write it as `MEDIA :` so a snooze never carries one."""
    return text.replace("MEDIA:", "MEDIA :") if text else text


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


def _parse_id(token: str) -> int | None:
    token = token.lstrip("#")
    return int(token) if token.isdigit() else None


def _duration(text: str) -> timedelta | None:
    """A duration such as "30m" or "1h30m" of at least one minute and at most `MAX_DURATION`, else None."""
    delta = _parse.parse_duration(text.strip())
    return delta if delta and _parse.MIN_DURATION <= delta <= _parse.MAX_DURATION else None


def _minutes_arg(token: str) -> float | None:
    """`later` delay: a bare number is minutes, or a duration such as 30m / 1h. None if invalid or under a minute."""
    if token.isdigit():
        return int(token) if int(token) >= 1 else None
    delta = _duration(token)
    return echo.minutes_value(delta) if delta else None


_BAD_CONFIRM = "confirm must be one word, an emoji, or options separated by |."


def _confirm_rule(value: Any) -> ConfirmRule | str:
    """A ConfirmRule from the tool's `confirm` value, or an error message."""
    if isinstance(value, dict):
        kind, text = str(value.get("kind") or "ok").lower(), str(value.get("value") or "")
        if kind not in ("ok", "word", "emoji", "choice"):
            return f"confirm kind {kind!r} is not one of ok, word, emoji, choice."
        if kind == "choice":
            rule = _parse.parse_ok_arg(text)
            return rule if isinstance(rule, ConfirmRule) and rule.kind == "choice" else _BAD_CONFIRM
        if kind in ("word", "emoji") and not text.strip():
            return f"confirm kind {kind!r} needs a value."
        return ConfirmRule(kind, "" if kind == "ok" else text)
    text = str(value if value is not None else "").strip()
    if text.lower() in ("", "ok"):
        return ConfirmRule()
    if len(text.split()) != 1:
        return _BAD_CONFIRM
    rule = _parse.parse_ok_arg(text)
    return _BAD_CONFIRM if isinstance(rule, ParseError) else rule


# -- Hermes access (imports only inside the helpers) ---------------------------


def hermes_data_dir() -> Path:
    """The plugin's data directory for the active profile."""
    from plugins.plugin_storage import plugin_data_dir

    return plugin_data_dir(PLUGIN_NAME)


def hermes_session_env(name: str) -> str:
    """A `HERMES_SESSION_*` value of the current session ("" if unset)."""
    try:
        from gateway.session_context import get_session_env

        return get_session_env(name, "")
    except Exception:
        return os.environ.get(name, "")


def hermes_paused() -> bool:
    from .send import hermes_is_paused  # send imports this module: lazy, not at the top

    return hermes_is_paused()


def hermes_gateway_running() -> bool | None:
    """Whether a gateway serves the active profile: True, False, or None if it cannot be told.

    Same check as `hermes profile show` (`hermes_cli/profiles.py:743-754`, `_check_gateway_running`):
    `gateway.status.resolve_gateway_liveness` (`gateway/status.py:1378`) = the profile's `gateway.pid`
    checked against its `gateway.lock`, then the runtime status, then a default-profile multiplexer
    that serves this profile. `cleanup_stale=False`: a probe never deletes files.
    """
    from .send import in_gateway  # send imports this module: lazy, not at the top

    if in_gateway():
        return True
    try:
        from gateway.status import get_running_pid, resolve_gateway_liveness
        from hermes_constants import get_hermes_home

        liveness = resolve_gateway_liveness(
            profile_dir=get_hermes_home(),
            use_cache=False,
            pid_probe=lambda path: get_running_pid(path, cleanup_stale=False),
        )
    except Exception:
        return None
    if not liveness.running and liveness.probe_error:
        return None
    return bool(liveness.running)


def platform_name(platform: Any) -> str:
    """A Hermes platform (enum or text) as a lower-case name."""
    return str(getattr(platform, "value", platform) or "").lower()


def _hermes_homes() -> list[Target]:
    """The home channel of every chat platform that has one, in config order."""
    homes = []
    try:
        from gateway.config import load_gateway_config

        config = load_gateway_config()
        for candidate, platform_config in config.platforms.items():
            home = getattr(platform_config, "home_channel", None)
            name = platform_name(candidate)
            if home is None or name in NON_CHAT_PLATFORMS:
                continue
            chat_id = str(home.chat_id)
            user_id = getattr(home, "user_id", None)
            if not user_id and name == "telegram" and chat_id.isdigit():
                user_id = chat_id  # a Telegram private chat has the user's id as its chat id
            homes.append(Target(platform=name, chat_id=chat_id, user_id=str(user_id) if user_id else None))
    except Exception:
        return []
    return homes


def hermes_home_target(platform: str | None = None) -> Target | None:
    """The requested platform's home channel, or the first configured chat home."""
    selected_platform = platform.lower() if platform else None
    return next((home for home in _hermes_homes() if selected_platform in (None, home.platform)), None)


def hermes_home_platforms() -> list[str]:
    """The chat platforms that have a home channel."""
    return [home.platform for home in _hermes_homes()]


def target_from_source(source: Any) -> Target | None:
    """The chat a gateway command came from, or None for the CLI / TUI / Desktop (no source)."""
    if source is None:
        return None
    name = platform_name(getattr(source, "platform", None))
    chat_id = getattr(source, "chat_id", None)
    if name in NON_CHAT_PLATFORMS or chat_id in (None, ""):
        return None
    user_id = getattr(source, "user_id", None)
    return Target(platform=name, chat_id=str(chat_id), user_id=str(user_id) if user_id else None)


# -- the commands ------------------------------------------------------------------


class Commands:
    """Handlers for `/snooze` and `snooze_create`, bound to a plugin context.

    The keyword arguments are the seams used by the tests; the defaults talk to Hermes.
    """

    def __init__(
        self,
        ctx: Any,
        *,
        data_dir: Callable[[], str | os.PathLike] = hermes_data_dir,
        now: Callable[[], datetime] = _utcnow,
        home_target: Callable[..., Target | None] = hermes_home_target,
        home_platforms: Callable[[], list[str]] = hermes_home_platforms,
        session_env: Callable[[str], str] = hermes_session_env,
        is_paused: Callable[[], bool] = hermes_paused,
        run_then: Callable[[Snooze, Any], None] | None = None,
        gateway_running: Callable[[], bool | None] = hermes_gateway_running,
    ):
        self._ctx = ctx
        self._data_dir = data_dir
        self._now = now
        self._home_target = home_target
        self._home_platforms = home_platforms
        self._session_env = session_env
        self._is_paused = is_paused
        self._run_then = run_then  # hands a confirmed snooze's `then` to the agent (hooks.Hooks.then_after_reply)
        self._gateway_running = gateway_running

    # -- entry points --------------------------------------------------------

    def command(self, raw_args: str, source: Any = None, **_context: Any) -> str:
        """Handler of `/snooze <args>`. Returns the reply text."""
        try:
            return self._dispatch((raw_args or "").strip(), source)
        except StoreError as exc:
            return f"⚠️ {exc}"
        except (OverflowError, ValueError):
            return OUT_OF_RANGE_TEXT

    def tool(self, args: Any, **_kwargs: Any) -> str:
        """Handler of the `snooze_create` tool. Returns a JSON string."""
        try:
            result = self._create_from_tool(args if isinstance(args, dict) else {})
        except StoreError as exc:
            result = {"error": str(exc)}
        except (OverflowError, ValueError):
            result = {"error": OUT_OF_RANGE_TEXT}
        return json.dumps(result, ensure_ascii=False)

    # -- command dispatch ------------------------------------------------------

    def _dispatch(self, text: str, source: Any) -> str:
        if not text:
            return self._help(source)
        name, *args = text.split()
        name = name.lower()
        if name == "test" and not args:
            return self._test(source)

        if name in SUBCOMMANDS:
            reply = self._cmd_ok(args, source) if name == "ok" else getattr(self, f"_cmd_{name}")(args)
            if reply is not None:
                return reply
            # The arguments do not fit the subcommand. "cancel the order at 4pm" is still a reminder.

        spec = self._parse(text)
        if isinstance(spec, ParseError):
            if name in SUBCOMMANDS:
                return USAGE[name]
            return f"⚠️ {spec.message}\nExample: {spec.example}\nNothing was set."
        target, where = self._target(spec.platform, target_from_source(source))
        if target is None:
            return self._no_target_text(spec.platform, FROM_CHAT, text)
        record, settings, now = self._add(spec, target)
        return self._echo(record, now, where, settings)

    def _several_platforms(self) -> bool:
        """Whether at least two chat platforms have a home channel: the user then has a platform to choose."""
        return len(self._home_platforms()) >= 2

    def _help(self, source: Any) -> str:
        """HELP; with several platforms, a line before the last one says where a snooze typed here goes."""
        platforms = self._home_platforms()
        if len(platforms) < 2:
            return HELP
        target, where = self._target(None, target_from_source(source))
        if target is None:
            hints = " or ".join(f'"via {name}"' for name in platforms)
            line = (
                f"Reminders from here: no default yet. Set one: /snooze platform {'|'.join(platforms)}, "
                f"or add {hints} to the text."
            )
        else:
            name = echo.platform_label(target.platform)
            place = f"this {name} chat" if where == WHERE_CHAT else f"{name} (home channel)"
            line = f"Reminders from here go to: {place}. Change: /snooze platform {'|'.join([*platforms, 'source'])}"
        head, guide = HELP.rsplit("\n", 1)
        return f"{head}\n{line}\n{guide}"

    def _parse(self, text: str) -> Spec | ParseError:
        return _parse.parse(text, self._now(), read_settings(self._ctx).timezone)

    def _store(self) -> Store:
        return Store(self._data_dir())

    # -- create ----------------------------------------------------------------

    def _target(self, via: str | None, chat: Target | None) -> tuple[Target | None, str]:
        """Where a new snooze goes: the `via` platform, else `notification_platform`, else `chat`, else the only home.

        None when no home channel fits, or when several platforms have one and there is no chat (the user must choose).
        """
        platform = via or read_settings(self._ctx).notification_platform
        if platform != "source":
            return self._home_target(platform), WHERE_HOME
        if chat is not None:
            return chat, WHERE_CHAT
        return (None if self._several_platforms() else self._home_target()), WHERE_HOME

    def _session_chat(self) -> Target | None:
        """The chat of the current agent session (tool, CLI), or None."""
        platform = self._session_env("HERMES_SESSION_PLATFORM").strip().lower()
        chat_id = self._session_env("HERMES_SESSION_CHAT_ID").strip()
        if platform in NON_CHAT_PLATFORMS or not chat_id:
            return None
        user_id = self._session_env("HERMES_SESSION_USER_ID").strip()
        return Target(platform=platform, chat_id=chat_id, user_id=user_id or None)

    def _no_target_text(self, via: str | None = None, origin: str = FROM_CHAT, text: str | None = None) -> str:
        """Why a snooze has no target. `origin` and `text` (None: none to repeat) shape the choose-a-platform reply."""
        if via:
            name = via.title()
            return (
                f"No {name} home channel is set for this profile. "
                f"Run /sethome in the {name} chat you want, then try again. Nothing was set."
            )
        platform = read_settings(self._ctx).notification_platform
        if platform != "source":
            name = platform.title()
            return (
                f"No {name} home channel is configured for this profile. "
                f"Set one with /sethome on {name} or choose another notification_platform. Nothing was set."
            )
        return self._choose_platform_text(origin, text) if self._several_platforms() else NO_TARGET_TEXT

    def _choose_platform_text(self, origin: str, text: str | None) -> str:
        """Several platforms have a home channel and the snooze came from no chat: say how to choose one."""
        platforms = self._home_platforms()
        first = platforms[0]
        if origin == FROM_TOOL:
            return (
                f"Several platforms have a home channel ({', '.join(platforms)}) and there is no chat. "
                "Ask the user which platform to use, then call snooze_create again with `platform`. Nothing was set."
            )
        names = [echo.platform_label(name) for name in platforms]
        setup = f"{', '.join(names[:-1])} and {names[-1]} are {'both' if len(names) == 2 else 'all'} set up."
        terminal = origin == FROM_TERMINAL
        lines = [
            f"⚠️ Where should reminders go? {setup}",
            f"Set a default once:  {'hermes snooze' if terminal else '/snooze'} platform {first}",
        ]
        if text:
            this_one = f'hermes snooze add "{text} via {first}"' if terminal else f"/snooze {text} via {first}"
            lines.append(f"Or for this one:     {this_one}")
        return "\n".join([*lines, "Nothing was set."])

    def _add(self, spec: Spec, target: Target, *, test: bool = False) -> tuple[Snooze, Settings, datetime]:
        """Store a new snooze for `spec`. Returns (record, settings, now).

        A `test` snooze starts at once: it has no `yes` step.
        """
        settings = read_settings(self._ctx)
        now = self._now()
        until = spec.until or spec.first_at + timedelta(minutes=settings.cap_minutes)
        active = test or not settings.validate_setup
        record = Snooze(
            text=_no_media_tag(spec.text),
            created=_iso(now),
            target=target,
            schedule=Schedule(
                first_at=_iso(spec.first_at),
                every=echo.minutes_value(spec.every or DEFAULT_EVERY),
                until=_iso(until),
                tz=spec.tz,
                days=sorted(spec.days) if spec.days else None,
                span=echo.minutes_value(until - spec.first_at) if spec.days else None,
            ),
            confirm={"kind": spec.confirm.kind, "value": spec.confirm.value},
            then=_no_media_tag(spec.then),
            state=ACTIVE if active else PENDING,
            next_at=_iso(spec.first_at) if active else None,
            test=test,
        )
        return self._store().add(record), settings, now

    def create_from_cli(self, text: str) -> tuple[Snooze | None, str]:
        """Create through the CLI using the same parser, target and store path as other entry points."""
        spec = self._parse(text)
        if isinstance(spec, ParseError):
            return None, f"{spec.message}\nExample: {spec.example}"

        target, where = self._target(spec.platform, self._session_chat())
        if target is None:
            return None, self._no_target_text(spec.platform, FROM_TERMINAL, text)

        try:
            record, settings, now = self._add(spec, target)
        except StoreError as exc:
            return None, str(exc)
        return record, self._echo(record, now, where, settings)

    def _echo(self, record: Snooze, now: datetime, where: str, settings: Settings) -> str:
        """The echo of a new snooze, plus the warning if no gateway runs for the profile (none if it cannot be told)."""
        off = self._gateway_running() is False
        return echo.render_echo(record, now, where, settings.cap_reminders, PENDING_TIMEOUT, gateway_off=off)

    def _create_from_tool(self, args: dict) -> dict:
        settings = read_settings(self._ctx)
        now = self._now()
        zone = echo.zone_of(settings.timezone)

        text = args.get("text")
        text = text.strip() if isinstance(text, str) else ""
        if not text:
            return {"error": "text is required: what to remind the user about."}

        first_at = self._tool_time(args.get("first_at"), "first_at", zone)
        if isinstance(first_at, str):
            return {"error": first_at}
        if first_at is None:
            return {"error": "first_at is required: an ISO 8601 date and time, for example 2026-11-07T16:00."}
        if first_at < now:
            return {"error": "first_at is in the past."}

        days = None
        recurrence = args.get("recurrence")
        if recurrence is not None:
            value = str(recurrence).strip().casefold()
            days = _parse.RECURRENCE_WORDS.get(value) or _parse.parse_days(value)
            if days is None:
                return {"error": "recurrence must be daily, weekdays, or days such as mon,thu or monday-friday."}

        until = self._tool_time(args.get("until"), "until", zone)
        if isinstance(until, str):
            return {"error": until}
        if until is not None and until <= first_at:
            return {"error": "until must be after first_at."}

        if days is not None and first_at.astimezone(zone).weekday() not in days:
            # Move first_at to the next recurrence day; until moves with it, so the span stays the same.
            local_first = first_at.astimezone(zone)
            moved = _parse.next_on_days(local_first.time().replace(tzinfo=None), first_at, zone, days)
            if until is not None:
                shift = moved.astimezone(zone).date() - local_first.date()
                local_until = until.astimezone(zone)
                until = datetime.combine(
                    local_until.date() + shift, local_until.time().replace(tzinfo=None), tzinfo=zone
                ).astimezone(timezone.utc)
            first_at = moved

        every = None
        if args.get("every") not in (None, ""):
            every = self._tool_every(args["every"])
            if every is None:
                return {
                    "error": "every must be a number of minutes or a duration such as 10m or 1h30m (1 min to 10 years)."
                }

        rule = _confirm_rule(args.get("confirm"))
        if isinstance(rule, str):
            return {"error": rule}

        then = args.get("then")
        then = then.strip() if isinstance(then, str) and then.strip() else None

        via = args.get("platform")
        if via not in (None, ""):
            via = str(via).strip().lower()
            if via not in _parse.PLATFORMS:
                return {"error": f"platform must be one of {', '.join(_parse.PLATFORMS)}."}
        else:
            via = None

        target, where = self._target(via, self._session_chat())
        if target is None:
            return {"error": self._no_target_text(via, FROM_TOOL)}

        spec = Spec(
            text=text,
            first_at=first_at,
            every=every,
            until=until,
            confirm=rule,
            then=then,
            tz=settings.timezone,
            days=days,
            platform=via,
        )
        record, settings, now = self._add(spec, target)
        started = record.state == ACTIVE
        return {
            "ok": True,
            "id": record.id,
            "state": record.state,
            "message": self._echo(record, now, where, settings),
            "note": (
                "Show `message` to the user as it is. "
                + (
                    "The snooze is running."
                    if started
                    else (
                        "It has NOT started: the user starts it with `yes` in a chat app, "
                        f"or with `/snooze yes {record.id}`."
                    )
                )
            ),
        }

    @staticmethod
    def _tool_time(value: Any, name: str, zone: tzinfo) -> datetime | str | None:
        """An ISO 8601 string as an aware UTC datetime; None if absent; an error message (str) if invalid."""
        if value in (None, ""):
            return None
        try:
            moment = datetime.fromisoformat(str(value).strip())
        except ValueError:
            return f"{name} must be an ISO 8601 date and time, for example 2026-11-07T16:00."
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=zone)
        return moment.astimezone(timezone.utc)

    @staticmethod
    def _tool_every(value: Any) -> timedelta | None:
        if isinstance(value, bool):
            return None
        if isinstance(value, (int, float)):
            delta = timedelta(minutes=value)
            return delta if _parse.MIN_DURATION <= delta <= _parse.MAX_DURATION else None
        text = str(value).strip()
        if text.isdigit():
            text += "m"
        return _duration(text)

    # -- subcommands -----------------------------------------------------------
    # Each `_cmd_*` takes the words after the name and returns the reply, or None when they do not fit its usage.

    def _cmd_list(self, rest: list) -> str | None:
        if rest:
            return None
        now = self._now()
        head = [PAUSED_TEXT] if self._is_paused() else []
        shown = [s for s in self._store().load() if s.state in (ACTIVE, PENDING)]
        if not shown:
            return "\n".join(head + ["No snoozes running."])
        lines = head + ["⏰ Snoozes"]
        several = self._several_platforms()
        for s in shown:
            zone = s.schedule.tz
            if s.state == PENDING:
                deadline = pending_deadline(s)
                if is_pending_expired(s, now):
                    detail = "waiting for yes, expired (not started)"
                else:
                    answer_by = f" (answer by {echo.format_when(deadline, zone, now)})" if deadline else ""
                    detail = f"waiting for yes{answer_by}"
            else:
                nxt = echo.from_iso(s.next_at)
                detail = f"next {echo.format_when(nxt, zone, now)}" if nxt else "no next reminder"
                if is_recurring(s):
                    detail = f"{echo.days_text(s.schedule.days)}, {detail}"
                detail += f", {s.sent_count} sent"
            if several:
                detail = f"{echo.platform_label(s.target.platform)}, {detail}"
            if s.last_error:
                detail += f" (last error: {s.last_error})"
            lines.append(f"#{s.id} {s.text} — {detail}")
        return "\n".join(lines)

    def platform(self, value: str | None, terminal: bool = False) -> tuple[bool, str]:
        """Show (`value` None) or set the default platform. Returns (whether it worked, the reply).

        `terminal`: the reply names the `hermes snooze` command instead of `/snooze`.
        """
        command = "hermes snooze" if terminal else "/snooze"
        if value is None:
            current = echo.default_platform_text(read_settings(self._ctx).notification_platform)
            return True, f"Default platform: {current}. Change: {command} platform telegram | discord | source"
        if value != "source" and self._home_target(value) is None:
            return False, self._no_target_text(value)
        try:
            self._ctx.set_config("notification_platform", value)
        except (OSError, ValueError) as exc:
            return False, f"⚠️ Could not save the setting: {exc}"
        others = "|".join(name for name in (*_parse.PLATFORMS, "source") if name != value)
        return True, f"✅ Reminders go to {echo.default_platform_text(value)}. Change: {command} platform {others}"

    def _cmd_platform(self, rest: list) -> str | None:
        if len(rest) > 1 or (rest and rest[0].lower() not in (*_parse.PLATFORMS, "source")):
            return None
        return self.platform(rest[0].lower() if rest else None)[1]

    def _cmd_test(self, rest: list) -> str | None:
        return None  # `/snooze test` alone is handled by `_test`; with words after it, it is a reminder

    def _test(self, source: Any) -> str:
        """`/snooze test`: one reminder now, to confirm with `ok` within `TEST_WINDOW`. See `docs/user-guide.md`.

        The command cannot wait for the reply, so it reports the check it can do now. With no gateway
        nothing would be sent, so no test is made.
        """
        target, where = self._target(None, target_from_source(source))
        if target is None:
            return self._no_target_text()
        running = self._gateway_running()
        if running is False:
            return echo.TEST_GATEWAY_OFF
        now = self._now()
        spec = Spec(
            text=echo.TEST_TEXT,
            first_at=now,
            every=echo.TEST_WINDOW,  # the next reminder would fall after `until`: the test sends one
            until=now + echo.TEST_WINDOW,
            confirm=ConfirmRule(),
            then=None,
            tz=read_settings(self._ctx).timezone,
        )
        record, *_ = self._add(spec, target, test=True)
        return echo.test_started_text(record, where, running)

    def _cmd_ok(self, rest: list, source: Any = None) -> str | None:
        sid = self._optional_id(rest)
        if sid is False:
            return None
        confirmed: list[Snooze] = []

        def run(txn: Txn) -> str:
            s, problem = self._select(txn, sid, ACTIVE)
            if problem:
                return problem
            options = echo.choice_options(s.confirm)
            if options:
                return f"#{s.id} needs an answer: reply {echo.numbered(options)} in the chat."
            reply = confirm_snooze(s, self._now())
            if s.then:
                confirmed.append(dataclasses.replace(s))
            return reply

        reply = self._store().update(run)
        if not confirmed:
            return reply
        if self._run_then is None or target_from_source(source) is None:
            # No chat to run the agent turn in (CLI / Desktop): see `docs/user-guide.md` ("Then-actions").
            if is_recurring(confirmed[0]):
                return f"{reply}\n{THEN_NOT_RUN_TEXT}"
            return f"✅ Done: {confirmed[0].text}\n{THEN_NOT_RUN_TEXT}"
        self._run_then(confirmed[0], source)
        return reply

    def _cmd_later(self, rest: list) -> str | None:
        if len(rest) > 2:
            return None
        now = self._now()
        minutes = None
        if rest:
            minutes = _minutes_arg(rest[0])
            if minutes is None:
                return None
        sid = None
        if len(rest) == 2:
            sid = _parse_id(rest[1])
            if sid is None:
                return None

        cap = read_settings(self._ctx).cap_reminders

        def run(txn: Txn) -> str:
            s, problem = self._select(txn, sid, ACTIVE)
            if problem:
                return problem
            delay = minutes if minutes is not None else (s.schedule.every or echo.minutes_value(DEFAULT_EVERY))
            return push_back_snooze(s, now, delay, cap)

        return self._store().update(run)

    def _cmd_cancel(self, rest: list) -> str | None:
        sid = _parse_id(rest[0]) if len(rest) == 1 else None
        if sid is None:
            return None

        def run(txn: Txn) -> str:
            s = txn.get(sid)
            if s is None:
                return f"No snooze #{sid}."
            if s.state not in (ACTIVE, PENDING):
                return f"Snooze #{sid} is already {s.state}."
            s.state, s.next_at = CANCELLED, None
            return f"🛑 Cancelled #{sid}: {s.text}"

        return self._store().update(run)

    def _cmd_yes(self, rest: list) -> str | None:
        sid = self._optional_id(rest)
        if sid is False:
            return None
        now = self._now()

        def run(txn: Txn) -> str:
            s, problem = self._select(txn, sid, PENDING)
            if problem:
                return problem
            return start_snooze(s, now)

        return self._store().update(run)

    # -- picking a snooze --------------------------------------------------------

    @staticmethod
    def _optional_id(rest: list) -> int | None | bool:
        """None for no id, the id, or False when the arguments are not `[id]`."""
        if not rest:
            return None
        if len(rest) > 1:
            return False
        sid = _parse_id(rest[0])
        return sid if sid is not None else False

    @staticmethod
    def _select(txn: Txn, sid: int | None, wanted: str) -> tuple[Snooze | None, str | None]:
        """The snooze to act on and, if there is none, the reply that explains why.

        With an id, any snooze is found and one in the wrong state is explained.
        Without one: the newest snooze in state `wanted` (for running ones, the
        one that sent a reminder last).
        """
        if sid is not None:
            s = txn.get(sid)
            if s is None:
                return None, f"No snooze #{sid}."
            if s.state == wanted:
                return s, None
            if s.state == PENDING:
                return None, (
                    f"Snooze #{sid} has not started: send `/snooze yes {sid}` to start it (or `/snooze cancel {sid}`)."
                )
            if s.state == ACTIVE:
                return None, f"Snooze #{sid} is already running."
            return None, f"Snooze #{sid} is already {s.state}."
        pool = [s for s in txn.snoozes if s.state == wanted]
        if not pool:
            return None, ("No snooze waiting for yes." if wanted == PENDING else "No running snooze.")
        if wanted == ACTIVE:
            epoch = datetime.min.replace(tzinfo=timezone.utc)
            return max(pool, key=lambda s: (echo.from_iso(s.last_sent_at) or epoch, s.id)), None
        return max(pool, key=lambda s: s.id), None


# -- registration ----------------------------------------------------------------

TOOL_SCHEMA = {
    "name": TOOL_NAME,
    "description": (
        "Set a snooze: a reminder that repeats in the user's chat until the user confirms it. "
        "The plugin sends every reminder itself, with no model call. Use the current date and "
        "time of the conversation to compute first_at. The snooze may need a `yes` from the user "
        "before it starts: show the returned `message` to the user as it is."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "text": {
                "type": "string",
                "description": "What to remind the user about, for example 'Send email to John Doe'.",
            },
            "first_at": {
                "type": "string",
                "description": (
                    "When the first reminder is sent: ISO 8601 date and time in the user's local time, "
                    "for example 2026-11-07T16:00."
                ),
            },
            "every": {
                "type": "string",
                "description": (
                    "Repeat interval: minutes as a number, or a duration such as '10m' or '1h30m'. "
                    "Minimum 1 minute. Default 10 minutes."
                ),
            },
            "until": {
                "type": "string",
                "description": "Stop time, ISO 8601, after first_at. Omit to use the plugin's default cap.",
            },
            "recurrence": {
                "type": "string",
                "description": (
                    "Optional. Repeat every week: 'daily' (or 'everyday', the same), 'weekdays', "
                    "or days such as 'mon,thu' or 'mon-fri'; full names work too ('monday,thursday', 'monday-friday'). "
                    "Each day starts a new occurrence at the time of first_at."
                ),
            },
            "platform": {
                "type": "string",
                "description": (
                    "Optional. Send this snooze to the home channel of that platform ('telegram' or 'discord') "
                    "instead of the current chat. Omit to use the current chat."
                ),
            },
            "confirm": {
                "type": "string",
                "description": (
                    "How the user confirms: 'ok' (default), a word or an emoji; or options separated by | "
                    "for a choice, for example 'asian|french|italian' (2 to 9 one-word options). "
                    "The whole reply must be exactly that (case and punctuation ignored); for a choice, "
                    "the user replies with an option or its number."
                ),
            },
            "then": {
                "type": "string",
                "description": (
                    "Optional action for the agent to do after the user confirms, for example 'close ticket 42'."
                ),
            },
        },
        "required": ["text", "first_at"],
        "additionalProperties": False,
    },
}


def register_all(ctx: Any, run_then: Callable[[Snooze, Any], None] | None = None) -> Commands:
    """Register the `/snooze` command, `snooze_create` tool and CLI command on `ctx`."""
    handlers = Commands(ctx, run_then=run_then)
    ctx.register_command(
        "snooze",
        handlers.command,
        description="A reminder that repeats until you confirm it: /snooze <what> at <time> every <duration>",
        args_hint=(
            "<what> (at <time> | in <duration>) [every <duration>] [daily | weekdays | every <days> | on <day>] "
            "[via <platform>] | list | ok | later | cancel | yes | test | platform"
        ),
    )
    ctx.register_tool(
        name=TOOL_NAME,
        toolset=PLUGIN_NAME,
        schema=TOOL_SCHEMA,
        handler=handlers.tool,
        description=TOOL_SCHEMA["description"],
        emoji="⏰",
    )
    from . import cli

    cli.register(ctx, handlers)
    return handlers
