"""Gateway hooks: read the user's answer to a reminder (ok, later, yes, a reaction).

Hermes glue: nothing from Hermes is imported at the top of this file, so the module
loads (and the tests run) without Hermes. What is accepted as an answer is decided by
`confirm.match`; the state changes are the ones of the `/snooze ok | later | yes`
commands (`commands.confirm_snooze`, `push_back_snooze`, `start_snooze`). The
behaviour is documented in the README ("Confirming").

Two hooks, both cheap when the chat has no running or waiting snooze (one small file
read, no lock):

- `pre_gateway_dispatch` (async): runs before authorization and before the busy-session
  lane, so an answer counts while the agent is busy. A pure answer is dropped (`skip`);
  "ok, and tell me a joke" is answered and still passes on to the agent. Slash commands
  are never read as an answer. If the snooze has a `then`, the message is rewritten into
  the agent turn instead (`THEN_PROMPT`).
- `gateway_platform_event` (sync): a Telegram reaction on the last reminder message. It
  carries no user id and no chat type, so only private chats are accepted: Telegram
  gives them positive chat ids. Hermes has already authorized the reactor.
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
import os
import threading
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any

from . import commands
from .confirm import CONFIRM, LATER, NONE, YES, Decision, Sender, match
from .send import HermesSender, start_timer
from .send import Sender as ReplySender
from .store import ACTIVE, PENDING, Snooze, Store, Target

log = logging.getLogger(__name__)

SKIP = {"action": "skip", "reason": "snooze: answer to a reminder"}

_background_tasks: set = set()  # strong references: the event loop keeps only weak ones


# What the agent receives (PLAN §5). The behaviour is documented in the README ("The then action").
THEN_PROMPT = (
    "The user confirmed the reminder '{text}'. Now do this: {then}. Then tell the user the result in one line."
)
INJECT_REFUSED = (
    "Hermes did not accept the turn. Set plugins.entries.snooze.allow_gateway_injection: true "
    "and make sure the gateway is running."
)
REPLY_GRACE_SECONDS = 1.0  # a command's reply is sent after its handler returns: let it go first


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _live_in_chat(snooze: Snooze, platform: str, chat_id: str) -> bool:
    target = snooze.target
    return (
        snooze.state in (ACTIVE, PENDING)
        and str(target.platform).lower() == platform
        and str(target.chat_id) == chat_id
    )


def _is_telegram_private(chat_id: str) -> bool:
    """Telegram gives private chats positive ids; groups, supergroups and channels negative ones."""
    return chat_id.isdigit()


def hermes_authorizes(gateway: Any, source: Any) -> bool:
    """Whether Hermes would let `source`'s sender use the bot (allowed users, pairing...). False when unsure.

    `pre_gateway_dispatch` runs before the gateway's own check, so for a snooze with no user
    id we ask the gateway (`GatewayRunner._is_user_authorized_for_source`).
    """
    check = getattr(gateway, "_is_user_authorized_for_source", None)
    if check is None:
        return False
    try:
        return check(source) is True
    except Exception:
        log.warning("snooze: could not ask Hermes whether the sender is authorized", exc_info=True)
        return False


def then_origin(target: Target, source: Any = None) -> dict:
    """The chat the agent turn runs in, in the `SessionSource.to_dict()` shape `inject_message` takes.

    The reminder's chat is the target. When the confirming message comes from that same chat, its
    own chat type, user and thread are used, so the turn lands in the session the user is talking
    in. Without a source (a reaction) a private chat is assumed: reactions are accepted in those only.
    """
    origin: dict = {"platform": str(target.platform).lower(), "chat_id": str(target.chat_id), "chat_type": "dm"}
    if target.user_id:
        origin["user_id"] = str(target.user_id)
    if source is not None:
        platform = str(getattr(source.platform, "value", source.platform) or "").lower()
        if platform == origin["platform"] and str(source.chat_id) == origin["chat_id"]:
            for name in ("chat_type", "user_id", "user_name", "thread_id"):
                value = getattr(source, name, None)
                if value:
                    origin[name] = str(value)
    return origin


class Hooks:
    """The two gateway hooks, and the `then` action. The keyword arguments are the seams used by the tests."""

    def __init__(
        self,
        ctx: Any = None,
        *,
        inject: Callable[..., bool] | None = None,
        reply_grace: float = REPLY_GRACE_SECONDS,
        data_dir: Callable[[], str | os.PathLike] = commands.hermes_data_dir,
        now: Callable[[], datetime] = _utcnow,
        authorizes: Callable[[Any, Any], bool] = hermes_authorizes,
        sender: ReplySender | None = None,
        ensure_timer: Callable[[], Any] | None = None,
    ):
        self._ensure_timer = ensure_timer or (lambda: start_timer(ctx))
        self._data_dir = data_dir
        self._now = now
        self._authorizes = authorizes
        self._sender = sender or HermesSender()
        self._inject = inject or getattr(ctx, "inject_message", None)
        self._reply_grace = reply_grace

    # -- pre_gateway_dispatch ---------------------------------------------------

    async def pre_gateway_dispatch(self, event: Any = None, gateway: Any = None, **_context: Any) -> dict | None:
        """Hermes hook: `{"action": "skip"}` for a pure answer, None to let the message through."""
        # This runs on the gateway loop, inside the profile's scope: the place `register()` cannot be
        # (it runs on a loop-less loader thread), so the reminder timer is started from here. Idempotent.
        try:
            self._ensure_timer()
        except Exception:
            log.warning("snooze: could not start the reminder timer", exc_info=True)
        try:
            return await self._on_message(event, gateway)
        except Exception:
            log.warning("snooze: pre_gateway_dispatch failed, message passed on", exc_info=True)
            return None

    async def _on_message(self, event: Any, gateway: Any) -> dict | None:
        source = getattr(event, "source", None)
        text = getattr(event, "text", None)
        if source is None or not isinstance(text, str) or not text.strip() or text.lstrip().startswith("/"):
            return None
        platform = str(getattr(source.platform, "value", source.platform) or "").lower()
        chat_id = str(source.chat_id)

        snoozes = [s for s in Store(self._data_dir()).load() if _live_in_chat(s, platform, chat_id)]
        if not snoozes:
            return None
        user_id = str(source.user_id) if source.user_id else None
        sender = Sender(platform, chat_id, user_id, source.chat_type)
        reply_to = getattr(event, "reply_to_message_id", None)

        decision = self._decide(text, snoozes, sender, reply_to, lambda: self._authorizes(gateway, source))
        if decision.action == NONE:
            return None
        applied = self._apply(decision)
        if applied is None:
            return None  # the snooze changed since it was read: let the message through
        reply, snooze = applied
        await self._reply(snooze.target, reply)
        if decision.action == CONFIRM and snooze.then:
            # The user's own message becomes the agent turn: right session, normal authorization, and
            # no `allow_gateway_injection` needed (gateway/run_inbound_hooks.py:69-110, the `rewrite` action).
            prompt = THEN_PROMPT.format(text=snooze.text, then=snooze.then)
            if decision.pass_through:
                prompt += f"\nThe user also wrote: {text}"
            return {"action": "rewrite", "text": prompt}
        return None if decision.pass_through else SKIP

    @staticmethod
    def _decide(
        text: str, snoozes: list[Snooze], sender: Sender, reply_to: Any, authorizes: Callable[[], bool]
    ) -> Decision:
        """`confirm.match`, also for snoozes that have no user id (created for a home channel).

        Those accept the sender only if Hermes authorizes them: the check runs once, and only
        when such a snooze is the one that matched.
        """
        open_ids = {s.id for s in snoozes if s.target.user_id is None}
        if not open_ids:
            return match(text, snoozes, sender, reply_to)
        provisional = [
            dataclasses.replace(s, target=dataclasses.replace(s.target, user_id=sender.user_id))
            if s.id in open_ids
            else s
            for s in snoozes
        ]
        decision = match(text, provisional, sender, reply_to)
        if decision.snooze_id in open_ids and not authorizes():
            return match(text, [s for s in snoozes if s.id not in open_ids], sender, reply_to)
        return decision

    # -- gateway_platform_event ---------------------------------------------------

    def gateway_platform_event(self, platform: str = "", event_type: str = "", payload: Any = None, **_: Any) -> None:
        """Hermes hook: a Telegram reaction on the last reminder message confirms that snooze."""
        if platform != "telegram" or event_type != "reaction" or not isinstance(payload, dict):
            return
        try:
            self._on_reaction(payload)
        except Exception:
            log.warning("snooze: gateway_platform_event failed", exc_info=True)

    def _on_reaction(self, payload: dict) -> None:
        chat_id = str(payload.get("chat_id") or "")
        message_id = str(payload.get("message_id") or "")
        emojis = payload.get("emojis")
        if not chat_id or not message_id or not emojis or not _is_telegram_private(chat_id):
            return
        snoozes = [
            s
            for s in Store(self._data_dir()).load()
            if _live_in_chat(s, "telegram", chat_id) and str(s.last_message_id) == message_id
        ]
        if not snoozes:
            return
        sender = Sender("telegram", chat_id, None, "dm")
        decision = match(emojis, snoozes, sender, reply_to_id=message_id, is_reaction=True)
        if decision.action != CONFIRM:
            return
        applied = self._apply(decision)
        if applied is None:
            return
        reply, snooze = applied
        self._spawn(self._reply_then(snooze, reply))

    async def _reply_then(self, snooze: Snooze, reply: str) -> None:
        """Send the reply, then run the `then` action: in this order, in one task."""
        await self._reply(snooze.target, reply)
        await self.run_then(snooze)

    def _spawn(self, coro: Any) -> None:
        """Run `coro` as a task on the running gateway loop, which keeps the profile scope."""
        try:
            task = asyncio.get_running_loop().create_task(coro)
        except RuntimeError:
            coro.close()
            log.warning("snooze: no running loop to send the reply")
            return
        _background_tasks.add(task)
        task.add_done_callback(_background_tasks.discard)

    async def _reply(self, target: Target, text: str) -> None:
        """Send `text` to the chat the reminder went to, the way the timer sends (no LLM, the active profile's bot).

        The send blocks, so it runs in a thread; `to_thread` copies the context, so the profile scope stays.
        A failed send is logged: the state change is already saved.
        """
        try:
            await asyncio.to_thread(self._sender.send, target, text)
        except Exception:
            log.warning("snooze: could not send the reply", exc_info=True)

    # -- the state change ---------------------------------------------------------

    def _apply(self, decision: Decision) -> tuple[str, Snooze] | None:
        """Apply `decision` to the stored snooze. Returns (reply text, the record), or None if it no longer fits."""
        now = self._now()
        wanted = PENDING if decision.action == YES else ACTIVE

        def run(txn):
            snooze = txn.get(decision.snooze_id)
            if snooze is None or snooze.state != wanted:
                return None
            if decision.action == CONFIRM:
                reply = commands.confirm_snooze(snooze)
            elif decision.action == LATER:
                reply = commands.push_back_snooze(snooze, now, decision.later_minutes)
            else:
                reply = commands.start_snooze(snooze, now)
            return reply, dataclasses.replace(snooze)

        return Store(self._data_dir()).update(run)

    # -- the then action ----------------------------------------------------------

    def _inject_then(self, snooze: Snooze, source: Any = None) -> str | None:
        """Queue the agent turn for a confirmed snooze. None if Hermes took it, else why not."""
        if self._inject is None:
            return "this Hermes has no inject_message"
        try:
            prompt = THEN_PROMPT.format(text=snooze.text, then=snooze.then)
            accepted = self._inject(prompt, origin=then_origin(snooze.target, source))
        except Exception as exc:
            log.warning("snooze: then-action failed", exc_info=True)
            return f"{type(exc).__name__}: {exc}"
        return None if accepted else INJECT_REFUSED

    async def run_then(self, snooze: Snooze, source: Any = None) -> None:
        """Hand the `then` action of a confirmed snooze to the agent, in the reminder's chat.

        For the paths with no user message to rewrite (a reaction): a text confirm rewrites the
        message instead (`_on_message`). Called after "✅ Done" is sent, on the gateway loop.
        `inject_message` queues one agent turn in that chat (in this plugin's own profile); it needs
        `allow_gateway_injection` and only works inside the gateway. If Hermes refuses or fails, the chat gets
        "⚠️ then-action failed: <reason>". See the README ("The then action").
        """
        if not snooze.then:
            return
        reason = self._inject_then(snooze, source)
        if reason:
            await self._reply(snooze.target, f"⚠️ then-action failed: {reason}")

    def then_after_reply(self, snooze: Snooze, source: Any = None) -> None:
        """`run_then` for a `/snooze ok` command, which runs in a worker thread.

        The command's reply is sent after its handler returns, so the action waits a moment.
        """
        timer = threading.Timer(self._reply_grace, self._then_in_thread, (snooze, source))
        timer.daemon = True
        timer.start()

    def _then_in_thread(self, snooze: Snooze, source: Any) -> None:
        reason = self._inject_then(snooze, source) if snooze.then else None
        if not reason:
            return
        try:
            self._sender.send(snooze.target, f"⚠️ then-action failed: {reason}")
        except Exception:
            log.warning("snooze: could not send the then-action failure", exc_info=True)


# -- registration ----------------------------------------------------------------------

HOOK_NAMES = ("pre_gateway_dispatch", "gateway_platform_event")


def register_all(ctx: Any) -> Hooks:
    """Register the gateway hooks on `ctx` (`HOOK_NAMES`, also listed in plugin.yaml)."""
    hooks = Hooks(ctx)
    ctx.register_hook("pre_gateway_dispatch", hooks.pre_gateway_dispatch)
    ctx.register_hook("gateway_platform_event", hooks.gateway_platform_event)
    return hooks
