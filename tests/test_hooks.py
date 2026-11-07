import asyncio
import contextvars
import enum
import re
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from snooze import hooks
from snooze.confirm import CONFIRM, LATER, Decision
from snooze.hooks import Hooks
from snooze.send import SendError
from snooze.store import ACTIVE, DONE, PENDING, Schedule, Snooze, Store, Target

NOW = datetime(2026, 11, 7, 10, 0, tzinfo=timezone.utc)
ROOT = Path(__file__).resolve().parent.parent


class Platform(enum.Enum):
    TELEGRAM = "telegram"
    DISCORD = "discord"


class FakeSender:
    def __init__(self, fail=False):
        self.sent = []  # (platform, chat_id, text)
        self.fail = fail
        self.threads = []

    def send(self, target, text):
        self.threads.append(threading.current_thread())
        if self.fail:
            raise SendError("network down")
        self.sent.append((target.platform, target.chat_id, text))
        return "1"


def gateway(authorized=False):
    return SimpleNamespace(_is_user_authorized_for_source=lambda source: authorized)


def event(text, chat="123", user="42", platform=Platform.TELEGRAM, chat_type="dm", reply_to=None):
    source = SimpleNamespace(platform=platform, chat_id=chat, user_id=user, chat_type=chat_type, thread_id=None)
    return SimpleNamespace(text=text, source=source, reply_to_message_id=reply_to)


class Base(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name) / "data"
        self.store = Store(self.dir)
        self.auth_calls = 0
        self.authorized = False
        self.sender = FakeSender()
        self.hooks = Hooks(
            data_dir=lambda: self.dir,
            now=lambda: NOW,
            authorizes=self._authorizes,
            sender=self.sender,
        )
        then = mock.patch.object(hooks, "run_then")
        self.then = then.start()
        self.addCleanup(then.stop)

    def _authorizes(self, gw, source):
        self.auth_calls += 1
        return self.authorized

    def add(self, state=ACTIVE, **kw):
        fields = dict(
            text="Wake up",
            created=NOW.isoformat(),
            target=Target("telegram", "123", "42"),
            schedule=Schedule(first_at=(NOW + timedelta(minutes=5)).isoformat(), every=10, tz="UTC"),
            state=state,
            last_sent_at=NOW.isoformat() if state == ACTIVE else None,
            sent_count=1 if state == ACTIVE else 0,
            last_message_id="777" if state == ACTIVE else None,
            next_at=(NOW + timedelta(minutes=10)).isoformat() if state == ACTIVE else None,
            confirm={"kind": "ok", "value": ""},
        )
        fields.update(kw)
        return self.store.add(Snooze(**fields))

    def stored(self, snooze):
        return self.store.get(snooze.id)

    async def dispatch(self, ev, gw=None):
        return await self.hooks.pre_gateway_dispatch(event=ev, gateway=gw or gateway())

    def sent(self):
        return [(chat, text) for _, chat, text in self.sender.sent]


class DispatchTest(Base):
    async def test_pure_confirm_is_skipped_and_done(self):
        s = self.add()
        result = await self.dispatch(event("ok"))
        self.assertEqual(result["action"], "skip")
        self.assertEqual(self.stored(s).state, DONE)
        self.assertEqual(self.sent(), [("123", "✅ Done: Wake up")])
        self.then.assert_called_once()
        self.assertEqual(self.then.call_args.args[0].id, s.id)

    async def test_confirm_with_more_text_passes_through(self):
        s = self.add()
        result = await self.dispatch(event("ok, and tell me a joke"))
        self.assertIsNone(result)
        self.assertEqual(self.stored(s).state, DONE)
        self.assertEqual(self.sent(), [("123", "✅ Done: Wake up")])

    async def test_later_pushes_back_and_is_skipped(self):
        s = self.add()
        result = await self.dispatch(event("later 2"))
        self.assertEqual(result["action"], "skip")
        self.assertEqual(self.stored(s).state, ACTIVE)
        self.assertEqual(self.stored(s).next_at, (NOW + timedelta(minutes=2)).isoformat())
        self.assertTrue(self.sent()[0][1].startswith("⏰ OK, back at"))
        self.then.assert_not_called()

    async def test_yes_starts_a_pending_snooze(self):
        s = self.add(state=PENDING)
        result = await self.dispatch(event("yes"))
        self.assertEqual(result["action"], "skip")
        self.assertEqual(self.stored(s).state, ACTIVE)
        self.assertTrue(self.sent()[0][1].startswith("▶️ Started"))
        self.then.assert_not_called()

    async def test_expired_yes_is_not_started(self):
        s = self.add(state=PENDING, created=(NOW - timedelta(hours=1)).isoformat())
        await self.dispatch(event("yes"))
        self.assertEqual(self.stored(s).state, "missed")
        self.assertEqual(self.sent(), [("123", "⌛ Not started: Wake up")])

    async def test_no_snooze_means_no_work(self):
        self.assertIsNone(await self.dispatch(event("ok")))
        self.assertFalse(self.dir.exists())  # not even a lock file
        self.assertEqual(self.sent(), [])

    async def test_other_chat_is_ignored(self):
        s = self.add()
        self.assertIsNone(await self.dispatch(event("ok", chat="555")))
        self.assertEqual(self.stored(s).state, ACTIVE)

    async def test_other_user_is_ignored(self):
        s = self.add()
        self.assertIsNone(await self.dispatch(event("ok", user="99")))
        self.assertEqual(self.stored(s).state, ACTIVE)
        self.assertEqual(self.sent(), [])

    async def test_other_platform_is_ignored(self):
        s = self.add()
        self.assertIsNone(await self.dispatch(event("ok", platform=Platform.DISCORD)))
        self.assertEqual(self.stored(s).state, ACTIVE)

    async def test_unrelated_message_passes_untouched(self):
        s = self.add()
        self.assertIsNone(await self.dispatch(event("what is the weather")))
        self.assertEqual(self.stored(s).state, ACTIVE)
        self.assertEqual(self.sent(), [])

    async def test_slash_command_is_never_an_answer(self):
        s = self.add(confirm={"kind": "any", "value": ""})
        self.assertIsNone(await self.dispatch(event("/snooze list")))
        self.assertEqual(self.stored(s).state, ACTIVE)

    async def test_reply_to_picks_that_snooze(self):
        first = self.add(text="First", last_message_id="10")
        second = self.add(text="Second", last_message_id="20", last_sent_at=(NOW + timedelta(minutes=1)).isoformat())
        await self.dispatch(event("ok", reply_to="10"))
        self.assertEqual(self.stored(first).state, DONE)
        self.assertEqual(self.stored(second).state, ACTIVE)

    async def test_failed_send_keeps_the_state_change(self):
        s = self.add()
        self.sender.fail = True
        with self.assertLogs("snooze.hooks", "WARNING"):
            result = await self.dispatch(event("ok"))
        self.assertEqual(result["action"], "skip")
        self.assertEqual(self.stored(s).state, DONE)

    async def test_reply_goes_through_the_sender_off_the_event_loop(self):
        self.add()
        await self.dispatch(event("ok"))
        self.assertEqual(self.sender.sent, [("telegram", "123", "✅ Done: Wake up")])
        self.assertNotIn(threading.current_thread(), self.sender.threads)

    async def test_reply_keeps_the_context_of_the_hook(self):
        """The profile scope is a context variable: the sender must see it from its worker thread."""
        scope = contextvars.ContextVar("scope", default=None)
        seen = []
        self.sender.send = lambda target, text: seen.append(scope.get())
        self.add()
        scope.set("snooze-profile")
        await self.dispatch(event("ok"))
        self.assertEqual(seen, ["snooze-profile"])

    async def test_broken_store_lets_the_message_through(self):
        self.dir.mkdir()
        (self.dir / "snoozes.json").write_text("{not json")
        with self.assertLogs("snooze.hooks", "WARNING"):
            self.assertIsNone(await self.dispatch(event("ok")))

    async def test_changed_snooze_is_not_touched(self):
        s = self.add(state=DONE)
        self.assertIsNone(self.hooks._apply(Decision(CONFIRM, s.id, None, False)))
        self.assertIsNone(self.hooks._apply(Decision(LATER, s.id, 5, False)))
        self.assertIsNone(self.hooks._apply(Decision(CONFIRM, 999, None, False)))


class NoUserIdTest(Base):
    """A snooze sent to a home channel has no user id: Hermes decides who may answer."""

    def add(self, **kw):
        return super().add(target=Target("discord", "chan", None), **kw)

    async def ask(self, text="ok", user="42"):
        return await self.dispatch(event(text, chat="chan", user=user, platform=Platform.DISCORD, chat_type="group"))

    async def test_authorized_sender_confirms(self):
        self.authorized = True
        s = self.add()
        result = await self.ask()
        self.assertEqual(result["action"], "skip")
        self.assertEqual(self.stored(s).state, DONE)
        self.assertIsNone(self.stored(s).target.user_id)  # the record is not changed
        self.assertEqual(self.sent(), [("chan", "✅ Done: Wake up")])

    async def test_sender_hermes_refuses_is_ignored(self):
        s = self.add()
        self.assertIsNone(await self.ask())
        self.assertEqual(self.stored(s).state, ACTIVE)
        self.assertEqual(self.auth_calls, 1)

    async def test_no_check_when_the_message_is_not_an_answer(self):
        self.add()
        await self.ask("good morning")
        self.assertEqual(self.auth_calls, 0)

    async def test_sender_without_user_id_is_ignored(self):
        self.authorized = True
        s = self.add()
        self.assertIsNone(await self.ask(user=None))
        self.assertEqual(self.stored(s).state, ACTIVE)

    async def test_refused_sender_does_not_hide_an_exact_snooze(self):
        loose = self.add(text="Loose", last_sent_at=(NOW + timedelta(minutes=1)).isoformat())
        exact = super().add(
            text="Exact", target=Target("discord", "chan", "42"), last_message_id="5", last_sent_at=NOW.isoformat()
        )
        await self.ask()
        self.assertEqual(self.stored(loose).state, ACTIVE)
        self.assertEqual(self.stored(exact).state, DONE)

    async def test_default_check_asks_the_gateway(self):
        ev = event("ok", chat="chan", user="42", platform=Platform.DISCORD)
        self.assertTrue(hooks.hermes_authorizes(gateway(authorized=True), ev.source))
        self.assertFalse(hooks.hermes_authorizes(gateway(authorized=False), ev.source))
        self.assertFalse(hooks.hermes_authorizes(SimpleNamespace(), ev.source))  # no such check: not authorized

        def broken(source):
            raise RuntimeError("boom")

        with self.assertLogs("snooze.hooks", "WARNING"):
            self.assertFalse(hooks.hermes_authorizes(SimpleNamespace(_is_user_authorized_for_source=broken), ev.source))


class ReactionTest(Base):
    async def react(self, emojis=("👍",), chat="123", message="777", platform="telegram", event_type="reaction"):
        payload = {"emojis": list(emojis), "custom_emoji_ids": [], "chat_id": chat, "message_id": message}
        self.hooks.gateway_platform_event(platform=platform, event_type=event_type, payload=payload)
        await asyncio.gather(*hooks._background_tasks)

    async def test_reaction_on_the_reminder_confirms(self):
        s = self.add(confirm={"kind": "emoji", "value": "👍"})
        await self.react()
        self.assertEqual(self.stored(s).state, DONE)
        self.assertEqual(self.sent(), [("123", "✅ Done: Wake up")])
        self.then.assert_called_once()

    async def test_reaction_with_the_default_ok_rule_does_not_confirm(self):
        s = self.add()
        await self.react()
        self.assertEqual(self.stored(s).state, ACTIVE)

    async def test_reaction_on_another_message_is_ignored(self):
        s = self.add(confirm={"kind": "emoji", "value": "👍"})
        await self.react(message="1")
        self.assertEqual(self.stored(s).state, ACTIVE)

    async def test_other_emoji_is_ignored(self):
        s = self.add(confirm={"kind": "emoji", "value": "👍"})
        await self.react(emojis=("❤",))
        self.assertEqual(self.stored(s).state, ACTIVE)

    async def test_removed_reaction_is_ignored(self):
        s = self.add(confirm={"kind": "emoji", "value": "👍"})
        await self.react(emojis=())
        self.assertEqual(self.stored(s).state, ACTIVE)

    async def test_group_chat_is_ignored(self):
        s = self.add(confirm={"kind": "emoji", "value": "👍"}, target=Target("telegram", "-100123", "42"))
        await self.react(chat="-100123")
        self.assertEqual(self.stored(s).state, ACTIVE)

    async def test_other_chat_is_ignored(self):
        s = self.add(confirm={"kind": "emoji", "value": "👍"})
        await self.react(chat="555")
        self.assertEqual(self.stored(s).state, ACTIVE)

    async def test_other_platform_or_event_is_ignored(self):
        s = self.add(confirm={"kind": "emoji", "value": "👍"})
        await self.react(platform="discord")
        await self.react(event_type="message_edited")
        self.assertEqual(self.stored(s).state, ACTIVE)

    async def test_no_snooze_means_no_work(self):
        await self.react()
        self.assertFalse(self.dir.exists())

    async def test_confirm_survives_a_failed_send(self):
        s = self.add(confirm={"kind": "emoji", "value": "👍"})
        self.sender.fail = True
        with self.assertLogs("snooze.hooks", "WARNING"):
            await self.react()
        self.assertEqual(self.stored(s).state, DONE)

    async def test_snooze_without_user_id_accepts_a_private_reaction(self):
        s = self.add(confirm={"kind": "emoji", "value": "👍"}, target=Target("telegram", "123", None))
        await self.react()
        self.assertEqual(self.stored(s).state, DONE)


class RegisterTest(unittest.TestCase):
    def test_registers_exactly_what_plugin_yaml_lists(self):
        registered = {}
        ctx = SimpleNamespace(register_hook=lambda name, callback: registered.update({name: callback}))
        hooks.register_all(ctx)
        manifest = (ROOT / "plugin.yaml").read_text()
        listed = re.search(r"^provides_hooks:\n((?:  - .+\n)+)", manifest, re.M).group(1)
        self.assertEqual(sorted(registered), sorted(re.findall(r"- (\S+)", listed)))
        self.assertEqual(sorted(registered), sorted(hooks.HOOK_NAMES))


if __name__ == "__main__":
    unittest.main()
