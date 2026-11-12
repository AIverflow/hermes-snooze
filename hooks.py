"""Gateway hooks: read the user's answer to a reminder (ok, later, yes, a reaction).

Hermes glue: nothing from Hermes is imported at the top of this file, so the module
loads (and the tests run) without Hermes. What is accepted as an answer is decided by
`confirm.match`; the state changes are the ones of the `/snooze ok | later | yes`
commands (`commands.confirm_snooze`, `push_back_snooze`, `start_snooze`). The
behaviour is documented in `docs/user-guide.md` ("Confirmations").

Three hooks, all cheap when the chat has no running or waiting snooze (one small file
read, no lock):

- `pre_gateway_dispatch` (async), for snoozes with an owner (`target.user_id`): runs before
  authorization and before the busy-session lane, so an answer counts while the agent is busy.
  An answer is dropped (`skip`), and so is a message that is not an answer but belongs to a
  reminder (a reply to it, or the first message after it): it gets the list of valid answers.
  Every other message passes on. Slash commands are never read as an answer. If the snooze has
  a `then`, the answer is rewritten into the agent turn instead (`then_prompt`).
- `post_gateway_admission` (async), for snoozes with no owner (made from the CLI for a home
  channel): Hermes has already admitted the sender, so any sender in the chat may answer. The
  same rules, without reply-to (the hook does not get it); a matched message is consumed
  (`handled`) and a `then` is queued with `inject_message`, as for a reaction.
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
from types import SimpleNamespace
from typing import Any

from . import commands, echo
from .confirm import CONFIRM, INVALID, LATER, NONE, YES, Decision, Sender, match
from .send import HermesSender, start_timer
from .send import Sender as ReplySender
from .store import ACTIVE, PENDING, Snooze, Store, Target

log = logging.getLogger(__name__)

SKIP = {"action": "skip", "reason": "snooze: answer to a reminder"}
HANDLED = {"action": "handled"}

_background_tasks: set = set()  # strong references: the event loop keeps only weak ones


# What the agent receives. See `docs/user-guide.md` ("Then-actions").
def then_prompt(snooze: Snooze) -> str:
    """Build the follow-up request shared by rewrite and injection paths."""
    answer = f" with the answer '{snooze.answer}'" if snooze.answer is not None else ""
    return (
        f"The user confirmed the reminder '{snooze.text}'{answer}. Now do this: {snooze.then}. "
        "Then tell the user the result in one line."
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


def _is_answer_text(text: Any) -> bool:
    """Whether `text` may be an answer: some text, not a slash command."""
    return isinstance(text, str) and bool(text.strip()) and not text.lstrip().startswith("/")


def _is_telegram_private(chat_id: str) -> bool:
    """Telegram gives private chats positive ids; groups, supergroups and channels negative ones."""
    return chat_id.isdigit()


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
        platform = commands.platform_name(source.platform)
        if platform == origin["platform"] and str(source.chat_id) == origin["chat_id"]:
            for name in ("chat_type", "user_id", "user_name", "thread_id"):
                value = getattr(source, name, None)
                if value:
                    origin[name] = str(value)
    return origin


class Hooks:
    """The three gateway hooks, and the `then` action. The keyword arguments are the seams used by the tests."""

    def __init__(
        self,
        ctx: Any = None,
        *,
        inject: Callable[..., bool] | None = None,
        reply_grace: float = REPLY_GRACE_SECONDS,
        data_dir: Callable[[], str | os.PathLike] = commands.hermes_data_dir,
        now: Callable[[], datetime] = _utcnow,
        sender: ReplySender | None = None,
        ensure_timer: Callable[[], Any] | None = None,
        cap_reminders: Callable[[], int] | None = None,
    ):
        self._ensure_timer = ensure_timer or (lambda: start_timer(ctx))
        self._data_dir = data_dir
        self._now = now
        self._sender = sender or HermesSender()
        self._inject = inject or getattr(ctx, "inject_message", None)
        self._reply_grace = reply_grace
        self._cap_reminders = cap_reminders or (
            (lambda: commands.read_settings(ctx).cap_reminders)
            if ctx is not None
            else (lambda: commands.DEFAULT_CAP_REMINDERS)
        )

    # -- pre_gateway_dispatch ---------------------------------------------------

    async def pre_gateway_dispatch(self, event: Any = None, gateway: Any = None, **_context: Any) -> dict | None:
        """Hermes hook: `{"action": "skip"}` for a pure answer, None to let the message through."""
        # Fallback start: `register()` starts the timer (or a waiter thread that does, once the gateway loop
        # is up). Should that not have happened yet, the first message starts it from here, on the loop,
        # and the waiter stands down. Idempotent.
        try:
            self._ensure_timer()
        except Exception:
            log.warning("snooze: could not start the reminder timer", exc_info=True)
        try:
            return await self._on_message(event)
        except Exception:
            log.warning("snooze: pre_gateway_dispatch failed, message passed on", exc_info=True)
            return None

    async def _on_message(self, event: Any) -> dict | None:
        source = getattr(event, "source", None)
        text = getattr(event, "text", None)
        if source is None or not _is_answer_text(text):
            return None
        platform = commands.platform_name(source.platform)
        chat_id = str(source.chat_id)

        snoozes = [
            s
            for s in Store(self._data_dir()).load()
            if _live_in_chat(s, platform, chat_id) and s.target.user_id is not None
        ]
        if not snoozes:
            return None  # a snooze with no owner waits for post_gateway_admission
        user_id = str(source.user_id) if source.user_id else None
        sender = Sender(platform, chat_id, user_id, source.chat_type)
        reply_to = getattr(event, "reply_to_message_id", None)

        decision = match(text, snoozes, sender, reply_to)
        if decision.action == NONE:
            return None
        applied = self._apply(decision, text)
        if applied is None:
            return None  # the snooze changed since it was read: let the message through
        reply, snooze = applied
        await self._reply(snooze.target, reply)
        if decision.action == CONFIRM and snooze.then:
            # The user's own message becomes the agent turn: right session, normal authorization, and
            # no `allow_gateway_injection` needed (gateway/run_inbound_hooks.py:69-110, the `rewrite` action).
            return {"action": "rewrite", "text": then_prompt(snooze)}
        return SKIP

    # -- post_gateway_admission ---------------------------------------------------

    async def post_gateway_admission(
        self, platform: str = "", source: Any = None, text: Any = None, **_: Any
    ) -> dict | None:
        """Hermes hook: `{"action": "handled"}` for an answer to a snooze with no owner, None to let it through."""
        try:
            return self._on_admitted(platform, source, text)
        except Exception:
            log.warning("snooze: post_gateway_admission failed, message passed on", exc_info=True)
            return None

    def _on_admitted(self, platform: str, source: Any, text: Any) -> dict | None:
        """`source` is `SessionSource.to_dict()`. The sender stands in as the user of each owner-less snooze."""
        if not isinstance(source, dict) or not _is_answer_text(text):
            return None
        platform = commands.platform_name(platform)
        chat_id = str(source.get("chat_id"))
        user_id = str(source["user_id"]) if source.get("user_id") else None
        snoozes = [
            dataclasses.replace(s, target=dataclasses.replace(s.target, user_id=user_id))
            for s in Store(self._data_dir()).load()
            if _live_in_chat(s, platform, chat_id) and s.target.user_id is None
        ]
        if not snoozes:
            return None
        decision = match(text, snoozes, Sender(platform, chat_id, user_id, source.get("chat_type")))
        if decision.action == NONE:
            return None
        applied = self._apply(decision, text)
        if applied is None:
            return None
        reply, snooze = applied
        # In a task: Hermes frees the chat's session once the hook returns, before the `then` turn is queued.
        if decision.action == CONFIRM:
            self._spawn(self._reply_then(snooze, reply, SimpleNamespace(**source)))
        else:
            self._spawn(self._reply(snooze.target, reply))
        return HANDLED

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

    async def _reply_then(self, snooze: Snooze, reply: str, source: Any = None) -> None:
        """Send the reply, then run the `then` action: in this order, in one task."""
        await self._reply(snooze.target, reply)
        await self.run_then(snooze, source)

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

    def _apply(self, decision: Decision, text: str = "") -> tuple[str, Snooze] | None:
        """Apply `decision` to the stored snooze. Returns (reply text, the record), or None if it no longer fits.

        `text` is the message as typed, quoted back for an invalid answer.
        """
        now = self._now()
        cap = self._cap_reminders()
        wanted = PENDING if decision.action == YES else ACTIVE

        def run(txn):
            snooze = txn.get(decision.snooze_id)
            if snooze is None or snooze.state != wanted:
                return None
            if wanted == ACTIVE:
                snooze.awaiting_answer = False  # a message was read: the next reminder arms it again
            if decision.action == INVALID:
                reply = echo.invalid_answer_text(snooze, text)
            elif decision.action == CONFIRM:
                reply = commands.confirm_snooze(snooze, now, decision.answer)
            elif decision.action == LATER:
                reply = commands.push_back_snooze(snooze, now, decision.later_minutes, cap)
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
            prompt = then_prompt(snooze)
            accepted = self._inject(prompt, origin=then_origin(snooze.target, source))
        except Exception as exc:
            log.warning("snooze: then-action failed", exc_info=True)
            return f"{type(exc).__name__}: {exc}"
        return None if accepted else INJECT_REFUSED

    async def run_then(self, snooze: Snooze, source: Any = None) -> None:
        """Hand the `then` action of a confirmed snooze to the agent, in the reminder's chat.

        For the paths with no user message to rewrite (a reaction, an answer in `post_gateway_admission`):
        a text confirm in `pre_gateway_dispatch` rewrites the message instead (`_on_message`). Called after
        "✅ Done" is sent, on the gateway loop. `inject_message` queues one agent turn in that chat (in this
        plugin's own profile); it needs `allow_gateway_injection` and only works inside the gateway. If Hermes
        refuses or fails, the chat gets "⚠️ then-action failed: <reason>". See `docs/user-guide.md`
        ("Then-actions").
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

HOOK_NAMES = ("pre_gateway_dispatch", "post_gateway_admission", "gateway_platform_event")


def register_all(ctx: Any) -> Hooks:
    """Register the gateway hooks on `ctx` (`HOOK_NAMES`, also listed in plugin.yaml)."""
    hooks = Hooks(ctx)
    ctx.register_hook("pre_gateway_dispatch", hooks.pre_gateway_dispatch)
    ctx.register_hook("post_gateway_admission", hooks.post_gateway_admission)
    ctx.register_hook("gateway_platform_event", hooks.gateway_platform_event)
    return hooks
