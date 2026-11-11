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
from .parse import ConfirmRule, ParseError, Spec
from .schedule import PENDING_TIMEOUT, apply_later, is_pending_expired, is_recurring, pending_deadline, rearm
from .store import ACTIVE, CANCELLED, DONE, MISSED, PENDING, Schedule, Snooze, Store, StoreError, Target, Txn

PLUGIN_NAME = "snooze"
TOOL_NAME = "snooze_create"

THEN_NOT_RUN_TEXT = "⚠️ The then-action was not run: it runs only when you confirm from the chat."

DEFAULT_EVERY = timedelta(minutes=10)  # used when the text gives no `every`

DEFAULT_VALIDATE_SETUP = False

# Where a snooze goes: the chat the command came from, or the profile's home channel.
WHERE_CHAT = "this chat"
WHERE_HOME = "home channel"
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

SUBCOMMANDS = ("list", "ok", "later", "cancel", "yes", "test")

USAGE = {
    "list": "Usage: /snooze list",
    "ok": "Usage: /snooze ok [id]",
    "later": "Usage: /snooze later [minutes] [id]   (for example: /snooze later 30)",
    "cancel": "Usage: /snooze cancel <id>",
    "yes": "Usage: /snooze yes [id]",
    "test": "Usage: /snooze test",
}

HELP = (
    "Usage: /snooze <what> (at <time> | in <duration>) [every <duration>] [until <time>] "
    "[daily | weekdays | on <days>] [ok <word>] [then <action>]\n"
    f"Short form: /snooze <what> <time> [<duration>], for example {_parse.EXAMPLE_SHORT}\n"
    f"Example: {_parse.EXAMPLE_FULL}\n"
    "Other commands: list, ok [id], later [minutes] [id], cancel <id>, yes [id], test"
)

PAUSED_TEXT = "⏸ Hermes is paused: no reminders until hermes resume"

NO_TARGET_TEXT = (
    "No chat to send reminders to: run this from a chat (Telegram, Discord, ...), "
    "or set a home channel for the profile. Nothing was set."
)

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
        "telegram",
        "discord",
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
    choice = isinstance(snooze.confirm, dict) and snooze.confirm.get("kind") == "choice"
    answer_text = f" → {answer}" if answer and choice else ""
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


_BAD_CONFIRM = "confirm must be one word, an emoji, `any` or `contains:<word>`."


def _confirm_rule(value: Any) -> ConfirmRule | str:
    """A ConfirmRule from the tool's `confirm` value, or an error message."""
    if isinstance(value, dict):
        kind, text = str(value.get("kind") or "ok").lower(), str(value.get("value") or "")
        if kind not in ("ok", "word", "contains", "any", "emoji", "choice"):
            return f"confirm kind {kind!r} is not one of ok, word, contains, any, emoji, choice."
        if kind == "choice":
            rule = _parse.parse_ok_arg(text)
            return rule if isinstance(rule, ConfirmRule) and rule.kind == "choice" else _BAD_CONFIRM
        if kind in ("word", "contains", "emoji") and not text.strip():
            return f"confirm kind {kind!r} needs a value."
        return ConfirmRule(kind, "" if kind in ("ok", "any") else text)
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


def hermes_home_target(platform: str | None = None) -> Target | None:
    """The requested platform's home channel, or the first configured chat home."""
    selected_platform = platform.lower() if platform else None
    try:
        from gateway.config import load_gateway_config

        config = load_gateway_config()
        for candidate, platform_config in config.platforms.items():
            home = getattr(platform_config, "home_channel", None)
            name = str(getattr(candidate, "value", candidate)).lower()
            if selected_platform is not None and name != selected_platform:
                continue
            if home is None or name in NON_CHAT_PLATFORMS:
                continue
            chat_id = str(home.chat_id)
            user_id = getattr(home, "user_id", None)
            if not user_id and name == "telegram" and chat_id.isdigit():
                user_id = chat_id  # a Telegram private chat has the user's id as its chat id
            return Target(platform=name, chat_id=chat_id, user_id=str(user_id) if user_id else None)
    except Exception:
        return None
    return None


def target_from_source(source: Any) -> Target | None:
    """The chat a gateway command came from, or None for the CLI / TUI / Desktop (no source)."""
    if source is None:
        return None
    platform = getattr(source, "platform", None)
    name = str(getattr(platform, "value", platform) or "").lower()
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
        session_env: Callable[[str], str] = hermes_session_env,
        is_paused: Callable[[], bool] = hermes_paused,
        run_then: Callable[[Snooze, Any], None] | None = None,
        gateway_running: Callable[[], bool | None] = hermes_gateway_running,
    ):
        self._ctx = ctx
        self._data_dir = data_dir
        self._now = now
        self._home_target = home_target
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
            return HELP
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
        return self._create_from_spec(spec, *self._command_target(source))

    def _parse(self, text: str) -> Spec | ParseError:
        return _parse.parse(text, self._now(), read_settings(self._ctx).timezone)

    def _store(self) -> Store:
        return Store(self._data_dir())

    # -- create ----------------------------------------------------------------

    def _command_target(self, source: Any) -> tuple[Target | None, str]:
        platform = read_settings(self._ctx).notification_platform
        if platform != "source":
            return self._home_target(platform), WHERE_HOME
        target = target_from_source(source)
        if target is not None:
            return target, WHERE_CHAT
        return self._home_target(), WHERE_HOME

    def _tool_target(self) -> tuple[Target | None, str]:
        platform = read_settings(self._ctx).notification_platform
        if platform != "source":
            return self._home_target(platform), WHERE_HOME
        platform = self._session_env("HERMES_SESSION_PLATFORM").strip().lower()
        chat_id = self._session_env("HERMES_SESSION_CHAT_ID").strip()
        if platform not in NON_CHAT_PLATFORMS and chat_id:
            user_id = self._session_env("HERMES_SESSION_USER_ID").strip()
            return Target(platform=platform, chat_id=chat_id, user_id=user_id or None), WHERE_CHAT
        return self._home_target(), WHERE_HOME

    def _no_target_text(self) -> str:
        platform = read_settings(self._ctx).notification_platform
        if platform != "source":
            name = platform.title()
            return (
                f"No {name} home channel is configured for this profile. "
                f"Set one with /sethome on {name} or choose another notification_platform. Nothing was set."
            )
        return NO_TARGET_TEXT

    def _add(self, spec: Spec, target: Target, *, test: bool = False) -> tuple[Snooze, bool, Settings, datetime]:
        """Store a new snooze for `spec`. Returns (record, whether its stop time is the cap, settings, now).

        A `test` snooze starts at once: it has no `yes` step.
        """
        settings = read_settings(self._ctx)
        now = self._now()
        capped = spec.until is None
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
        return self._store().add(record), capped, settings, now

    def _create_from_spec(self, spec: Spec, target: Target | None, where: str) -> str:
        if target is None:
            return self._no_target_text()
        record, capped, settings, now = self._add(spec, target)
        return self._echo(record, now, where, capped, settings)

    def create_from_cli(self, text: str) -> tuple[Snooze | None, str]:
        """Create through the CLI using the same parser, target and store path as other entry points."""
        spec = self._parse(text)
        if isinstance(spec, ParseError):
            return None, f"{spec.message}\nExample: {spec.example}"

        target, where = self._tool_target()
        if target is None:
            return None, self._no_target_text()

        try:
            record, capped, settings, now = self._add(spec, target)
        except StoreError as exc:
            return None, str(exc)
        return record, self._echo(record, now, where, capped, settings)

    def _echo(self, record: Snooze, now: datetime, where: str, capped: bool, settings: Settings) -> str:
        """The echo of a new snooze, plus the warning if no gateway runs for the profile (none if it cannot be told).

        `where` is WHERE_CHAT only when the snooze has a chat of its own (the echo's chat wording depends on it).
        """
        off = self._gateway_running() is False
        return echo.render_echo(
            record,
            now,
            where,
            capped,
            settings.cap_reminders,
            PENDING_TIMEOUT,
            gateway_off=off,
            from_chat=where == WHERE_CHAT,
        )

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
            if value == "daily":
                days = frozenset(range(7))
            elif value == "weekdays":
                days = frozenset(range(5))
            else:
                days = _parse.parse_days(value)
            if days is None:
                return {"error": "recurrence must be daily, weekdays, or days such as mon,thu or mon-fri."}

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

        target, where = self._tool_target()
        if target is None:
            return {"error": self._no_target_text()}

        spec = Spec(
            text=text,
            first_at=first_at,
            every=every,
            until=until,
            confirm=rule,
            then=then,
            tz=settings.timezone,
            days=days,
        )
        record, capped, settings, now = self._add(spec, target)
        started = record.state == ACTIVE
        return {
            "ok": True,
            "id": record.id,
            "state": record.state,
            "message": self._echo(record, now, where, capped, settings),
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
            if s.last_error:
                detail += f" (last error: {s.last_error})"
            lines.append(f"#{s.id} {s.text} — {detail}")
        return "\n".join(lines)

    def _cmd_test(self, rest: list) -> str | None:
        return None  # `/snooze test` alone is handled by `_test`; with words after it, it is a reminder

    def _test(self, source: Any) -> str:
        """`/snooze test`: one reminder now, to confirm with `ok` within `TEST_WINDOW`. See `docs/user-guide.md`.

        The command cannot wait for the reply, so it reports the check it can do now. With no gateway
        nothing would be sent, so no test is made.
        """
        target, where = self._command_target(source)
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
            if isinstance(s.confirm, dict) and s.confirm.get("kind") == "choice":
                options = [option.strip() for option in str(s.confirm.get("value", "")).split("|") if option.strip()]
                choices = ", ".join(f"{index} {option}" for index, option in enumerate(options, 1))
                return f"#{s.id} needs an answer: reply {choices} in the chat."
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
                    "Optional. Days to repeat on: 'daily', 'weekdays', or days such as 'mon,thu' or 'mon-fri'. "
                    "Each day starts a new occurrence at the time of first_at."
                ),
            },
            "confirm": {
                "type": "string",
                "description": (
                    "How the user confirms: 'ok' (default), a word, an emoji, 'any' (any message) or "
                    "'contains:<word>'; or options separated by | for a choice, for example "
                    "'asian|french|italian' (2 to 9 one-word options); "
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
            "<what> (at <time> | in <duration>) [every <duration>] [daily | weekdays | on <days>] "
            "| list | ok | later | cancel | yes | test"
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
