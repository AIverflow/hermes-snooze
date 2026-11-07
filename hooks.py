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
  are never read as an answer.
- `gateway_platform_event` (sync): a Telegram reaction on the last reminder message. It
  carries no user id and no chat type, so only private chats are accepted: Telegram
  gives them positive chat ids. Hermes has already authorized the reactor.
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
import os
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any

from . import commands
from .confirm import CONFIRM, LATER, NONE, YES, Decision, Sender, match
from .send import HermesSender
from .send import Sender as ReplySender
from .store import ACTIVE, PENDING, Snooze, Store, Target

log = logging.getLogger(__name__)

SKIP = {"action": "skip", "reason": "snooze: answer to a reminder"}

_background_tasks: set = set()  # strong references: the event loop keeps only weak ones


def run_then(snooze: Snooze) -> None:
    """Call point for the `then` action (T11): called once the user confirmed and "✅ Done" is sent."""


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


class Hooks:
    """The two gateway hooks. The keyword arguments are the seams used by the tests."""

    def __init__(
        self,
        *,
        data_dir: Callable[[], str | os.PathLike] = commands.hermes_data_dir,
        now: Callable[[], datetime] = _utcnow,
        authorizes: Callable[[Any, Any], bool] = hermes_authorizes,
        sender: ReplySender | None = None,
    ):
        self._data_dir = data_dir
        self._now = now
        self._authorizes = authorizes
        self._sender = sender or HermesSender()

    # -- pre_gateway_dispatch ---------------------------------------------------

    async def pre_gateway_dispatch(self, event: Any = None, gateway: Any = None, **_context: Any) -> dict | None:
        """Hermes hook: `{"action": "skip"}` for a pure answer, None to let the message through."""
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
        if decision.action == CONFIRM:
            run_then(snooze)
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
        self._send_in_background(snooze.target, reply)
        run_then(snooze)

    def _send_in_background(self, target: Target, text: str) -> None:
        """Reply from the sync hook: a task on the running gateway loop, which keeps the profile scope."""
        try:
            task = asyncio.get_running_loop().create_task(self._reply(target, text))
        except RuntimeError:
            log.warning("snooze: no running loop to send %r", text)
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


# -- registration ----------------------------------------------------------------------

HOOK_NAMES = ("pre_gateway_dispatch", "gateway_platform_event")


def register_all(ctx: Any) -> Hooks:
    """Register the gateway hooks on `ctx` (`HOOK_NAMES`, also listed in plugin.yaml)."""
    hooks = Hooks()
    ctx.register_hook("pre_gateway_dispatch", hooks.pre_gateway_dispatch)
    ctx.register_hook("gateway_platform_event", hooks.gateway_platform_event)
    return hooks
