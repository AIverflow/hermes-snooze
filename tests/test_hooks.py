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

from snooze import hooks, schedule
from snooze.confirm import CONFIRM, INVALID, LATER, Decision
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


class FakeInject:
    """Stands in for `ctx.inject_message`: records the turns, answers `accept`, or raises `error`."""

    def __init__(self, accept=True, error=None):
        self.calls = []  # (prompt, origin)
        self.accept = accept
        self.error = error

    def __call__(self, content, role="user", *, session_key=None, origin=None):
        if self.error:
            raise self.error
        self.calls.append((content, origin))
        return self.accept


def event(text, chat="123", user="42", platform=Platform.TELEGRAM, chat_type="dm", reply_to=None):
    source = SimpleNamespace(platform=platform, chat_id=chat, user_id=user, chat_type=chat_type, thread_id=None)
    return SimpleNamespace(text=text, source=source, reply_to_message_id=reply_to)


class Base(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name) / "data"
        self.store = Store(self.dir)
        self.sender = FakeSender()
        self.inject = FakeInject()
        self.timer_starts = 0
        self.hooks = Hooks(
            ensure_timer=self._ensure_timer,
            inject=self.inject,
            reply_grace=0,
            data_dir=lambda: self.dir,
            now=lambda: NOW,
            sender=self.sender,
        )

    def _ensure_timer(self):
        self.timer_starts += 1

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

    async def dispatch(self, ev):
        return await self.hooks.pre_gateway_dispatch(event=ev, gateway=SimpleNamespace())

    def sent(self):
        return [(chat, text) for _, chat, text in self.sender.sent]


EMOJI = {"kind": "emoji", "value": "👍"}


class TimerStartTest(Base):
    async def test_every_inbound_message_asks_for_the_timer_even_slash_commands(self):
        await self.hooks.pre_gateway_dispatch(event=event("hello"))
        await self.hooks.pre_gateway_dispatch(event=event("/snooze test"))
        self.assertEqual(self.timer_starts, 2)

    async def test_a_failing_timer_start_does_not_stop_the_message(self):
        def boom():
            raise RuntimeError("no")

        self.hooks._ensure_timer = boom
        self.add()
        with self.assertLogs("snooze.hooks", "WARNING"):
            result = await self.hooks.pre_gateway_dispatch(event=event("ok"))
        self.assertEqual(result, hooks.SKIP)


class ThenTest(Base):
    PROMPT = (
        "The user confirmed the reminder 'Wake up'. Now do this: start the coffee machine. "
        "Then tell the user the result in one line."
    )

    def with_then(self, **kw):
        return self.add(then="start the coffee machine", **kw)

    async def react(self):
        """Confirm with a reaction: the path that has no user message to rewrite, so it injects."""
        payload = {"emojis": ["👍"], "chat_id": "123", "message_id": "777"}
        self.hooks.gateway_platform_event(platform="telegram", event_type="reaction", payload=payload)
        await asyncio.gather(*hooks._background_tasks)

    async def test_text_confirm_says_done_then_rewrites_the_message_into_the_prompt(self):
        s = self.with_then()
        result = await self.dispatch(event("ok"))
        self.assertEqual(result, {"action": "rewrite", "text": self.PROMPT})
        self.assertEqual(self.stored(s).state, DONE)
        self.assertEqual(self.sent(), [("123", "✅ Done: Wake up. Now: start the coffee machine…")])
        self.assertEqual(self.inject.calls, [])  # no injection: needs no allow_gateway_injection

    async def test_recurring_text_confirm_rearms_and_keeps_then_rewrite(self):
        s = self.with_then(
            schedule=Schedule(
                first_at="2026-11-07T08:00:00+00:00",
                every=10,
                tz="Europe/Paris",
                days=list(range(7)),
                span=120,
            ),
            next_at=NOW.isoformat(),
        )
        result = await self.dispatch(event("ok"))
        stored = self.stored(s)
        self.assertEqual(result, {"action": "rewrite", "text": self.PROMPT})
        self.assertEqual((stored.state, stored.sent_count, stored.then, stored.text), (ACTIVE, 0, s.then, s.text))
        self.assertEqual(
            self.sent(),
            [("123", "✅ Done for today: Wake up. Now: start the coffee machine… Next: tomorrow 09:00")],
        )

    async def test_confirm_with_extra_text_is_not_an_answer_and_reaches_the_agent(self):
        s = self.with_then()
        self.assertIsNone(await self.dispatch(event("ok, and book a table")))
        self.assertEqual(self.stored(s).state, ACTIVE)
        self.assertEqual(self.sent(), [])
        self.assertEqual(self.inject.calls, [])

    async def test_text_confirm_without_then_is_unchanged(self):
        s = self.add()
        self.assertEqual((await self.dispatch(event("ok")))["action"], "skip")
        self.assertEqual(self.stored(s).state, DONE)
        self.assertEqual(self.inject.calls, [])

    async def test_command_origin_takes_the_chat_type_and_thread_of_the_confirming_message(self):
        s = self.with_then(target=Target("telegram", "-100", "42"))
        ev = event("ok", chat="-100", chat_type="group")
        ev.source.thread_id = "9"
        await self.hooks.run_then(self.stored(s), ev.source)
        self.assertEqual(
            self.inject.calls[0][1],
            {"platform": "telegram", "chat_id": "-100", "chat_type": "group", "user_id": "42", "thread_id": "9"},
        )
        self.assertEqual(self.inject.calls[0][0], self.PROMPT)

    async def test_done_is_sent_before_the_injection(self):
        self.with_then(confirm=EMOJI)
        order = []
        self.sender.send = lambda target, text: order.append("send")
        self.hooks._inject = lambda prompt, origin=None: order.append("inject") or True
        await self.react()
        self.assertEqual(order, ["send", "inject"])

    async def test_refused_injection_tells_the_chat(self):
        self.with_then(confirm=EMOJI)
        self.inject.accept = False
        await self.react()
        texts = [text for _, text in self.sent()]
        self.assertEqual(len(texts), 2)
        self.assertTrue(texts[0].startswith("✅ Done"))
        self.assertTrue(texts[1].startswith("⚠️ then-action failed: "))
        self.assertIn("allow_gateway_injection", texts[1])

    async def test_injection_error_tells_the_chat_the_reason(self):
        s = self.with_then(confirm=EMOJI)
        self.inject.error = RuntimeError("no gateway")
        with self.assertLogs("snooze.hooks", "WARNING"):
            await self.react()
        self.assertEqual(self.sent()[1], ("123", "⚠️ then-action failed: RuntimeError: no gateway"))
        self.assertEqual(self.stored(s).state, DONE)

    async def test_no_inject_api_tells_the_chat(self):
        self.with_then(confirm=EMOJI)
        self.hooks._inject = None
        await self.react()
        self.assertEqual(self.sent()[1], ("123", "⚠️ then-action failed: this Hermes has no inject_message"))

    async def test_failed_warning_send_is_only_logged(self):
        self.with_then(confirm=EMOJI)
        self.inject.accept = False
        self.sender.fail = True
        with self.assertLogs("snooze.hooks", "WARNING"):
            await self.react()

    async def test_later_and_yes_do_not_run_then(self):
        self.with_then()
        await self.dispatch(event("later 2"))
        self.assertEqual(self.inject.calls, [])

    async def test_reaction_confirm_sends_done_then_injects(self):
        self.with_then(confirm=EMOJI)
        await self.react()
        self.assertEqual(self.sent(), [("123", "✅ Done: Wake up. Now: start the coffee machine…")])
        self.assertEqual(self.inject.calls[0][0], self.PROMPT)
        self.assertEqual(self.inject.calls[0][1]["chat_type"], "dm")

    async def test_then_after_reply_runs_the_action_later_from_a_thread(self):
        s = self.with_then()
        done = threading.Event()
        self.hooks._inject = lambda prompt, origin=None: done.set() or True
        self.hooks.then_after_reply(self.stored(s), None)
        self.assertTrue(await asyncio.to_thread(done.wait, 5))

    async def test_warning_from_a_thread_is_a_plain_send(self):
        s = self.with_then()
        self.inject.accept = False
        await asyncio.to_thread(self.hooks._then_in_thread, self.stored(s), None)
        self.assertTrue(self.sent()[0][1].startswith("⚠️ then-action failed"))
        self.assertIsNot(self.sender.threads[0], threading.main_thread())

    async def test_default_inject_is_the_ctx_method(self):
        calls = []
        ctx = SimpleNamespace(inject_message=lambda *a, **k: calls.append((a, k)) or True)
        await Hooks(ctx, sender=self.sender).run_then(self.with_then())
        self.assertEqual(len(calls), 1)
        self.assertIn("origin", calls[0][1])


class DispatchTest(Base):
    async def test_pure_confirm_is_skipped_and_done(self):
        s = self.add()
        result = await self.dispatch(event("ok"))
        self.assertEqual(result["action"], "skip")
        self.assertEqual(self.stored(s).state, DONE)
        self.assertEqual(self.sent(), [("123", "✅ Done: Wake up")])
        self.assertEqual(self.inject.calls, [])  # no `then`: nothing for the agent

    async def test_confirm_with_more_text_is_not_an_answer(self):
        s = self.add()
        self.assertIsNone(await self.dispatch(event("ok, and tell me a joke")))
        self.assertEqual(self.stored(s).state, ACTIVE)
        self.assertEqual(self.sent(), [])

    async def test_choice_answer_is_saved_replied_and_given_to_then_action(self):
        s = self.add(
            text="Dinner?",
            then="order it",
            confirm={"kind": "choice", "value": "asian|french|italian"},
        )
        result = await self.dispatch(event("2"))
        stored = self.stored(s)
        self.assertEqual(stored.state, DONE)
        self.assertEqual(stored.answer, "french")
        self.assertIn("→ french", self.sent()[0][1])
        self.assertEqual(
            result,
            {
                "action": "rewrite",
                "text": "The user confirmed the reminder 'Dinner?' with the answer 'french'. "
                "Now do this: order it. Then tell the user the result in one line.",
            },
        )

    async def test_recurring_choice_reply_keeps_answer_and_adds_it_to_done_reply(self):
        s = self.add(
            text="Dinner?",
            then="order it",
            confirm={"kind": "choice", "value": "asian|french|italian"},
            schedule=Schedule(
                first_at="2026-11-07T08:00:00+00:00",
                every=10,
                tz="Europe/Paris",
                days=list(range(7)),
                span=120,
            ),
        )
        await self.dispatch(event("2"))
        stored = self.stored(s)
        self.assertEqual((stored.state, stored.answer), (ACTIVE, "french"))
        self.assertIn("→ french", self.sent()[0][1])

    async def test_nonmatching_choice_passes_through_and_keeps_reminding(self):
        s = self.add(confirm={"kind": "choice", "value": "asian|french|italian"})
        result = await self.dispatch(event("pizza"))
        self.assertIsNone(result)
        self.assertEqual(self.stored(s).state, ACTIVE)
        self.assertIsNone(self.stored(s).answer)
        self.assertEqual(self.sent(), [])

    async def test_later_pushes_back_and_is_skipped(self):
        s = self.add()
        result = await self.dispatch(event("later 2"))
        self.assertEqual(result["action"], "skip")
        self.assertEqual(self.stored(s).state, ACTIVE)
        self.assertEqual(self.stored(s).next_at, (NOW + timedelta(minutes=2)).isoformat())
        self.assertTrue(self.sent()[0][1].startswith("⏰ OK, back at"))
        self.assertEqual(self.inject.calls, [])

    async def test_later_reaching_until_exactly_is_followed_by_a_send_at_that_time(self):
        until = NOW + timedelta(minutes=10)
        s = self.add(schedule=Schedule(first_at=NOW.isoformat(), every=10, until=until.isoformat(), tz="UTC"))
        await self.dispatch(event("later 10"))
        stored = self.stored(s)
        self.assertEqual(stored.next_at, until.isoformat())
        self.assertEqual(schedule.due(stored, until, 10).kind, schedule.SEND)
        self.assertTrue(self.sent()[0][1].startswith("⏰ OK, back at 10:10"))

    async def test_later_refused_at_the_reminder_cap_changes_nothing(self):
        s = self.add(sent_count=10)
        before = self.stored(s)
        result = await self.dispatch(event("later 30"))
        self.assertEqual(result["action"], "skip")
        self.assertEqual(self.stored(s), before)
        reply = self.sent()[0][1]
        self.assertNotIn("back at", reply)
        self.assertIn("10 reminders", reply)
        self.assertEqual(self.inject.calls, [])

    async def test_later_uses_the_configured_reminder_cap(self):
        self.hooks = Hooks(
            ensure_timer=self._ensure_timer,
            inject=self.inject,
            reply_grace=0,
            data_dir=lambda: self.dir,
            now=lambda: NOW,
            sender=self.sender,
            cap_reminders=lambda: 2,
        )
        s = self.add(sent_count=2)
        before = self.stored(s)
        await self.dispatch(event("later 30"))
        self.assertEqual(self.stored(s), before)

    async def test_yes_starts_a_pending_snooze(self):
        s = self.add(state=PENDING)
        result = await self.dispatch(event("yes"))
        self.assertEqual(result["action"], "skip")
        self.assertEqual(self.stored(s).state, ACTIVE)
        self.assertTrue(self.sent()[0][1].startswith("▶️ Started"))
        self.assertEqual(self.inject.calls, [])

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
        s = self.add(awaiting_answer=True)
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
        self.assertIsNone(self.hooks._apply(Decision(CONFIRM, s.id)))
        self.assertIsNone(self.hooks._apply(Decision(LATER, s.id, 5)))
        self.assertIsNone(self.hooks._apply(Decision(INVALID, s.id), "hmm"))
        self.assertIsNone(self.hooks._apply(Decision(CONFIRM, 999)))


class InvalidAnswerTest(Base):
    """A message that is not an answer, but belongs to a reminder: the list, and it never reaches the agent."""

    LIST = (
        '"okkkk" is not a valid answer for #{id} (Wake up).\n'
        "Reply: Ok · later · later 15     Or type: /snooze ok {id} · /snooze cancel {id}"
    )

    async def test_quoted_invalid_reply_gets_the_list_and_is_skipped(self):
        s = self.add()
        result = await self.dispatch(event("okkkk", reply_to="777"))
        self.assertEqual(result, hooks.SKIP)
        self.assertEqual(self.sent(), [("123", self.LIST.format(id=s.id))])
        self.assertEqual(self.stored(s).state, ACTIVE)
        self.assertIsNone(self.stored(s).answer)

    async def test_first_message_after_a_reminder_gets_the_list_the_second_goes_to_the_agent(self):
        s = self.add(awaiting_answer=True)
        self.assertEqual(await self.dispatch(event("okkkk")), hooks.SKIP)
        self.assertEqual(self.sent(), [("123", self.LIST.format(id=s.id))])
        self.assertFalse(self.stored(s).awaiting_answer)
        self.assertIsNone(await self.dispatch(event("what is the weather")))
        self.assertEqual(len(self.sent()), 1)

    async def test_a_message_while_not_awaiting_goes_to_the_agent(self):
        self.add()
        self.assertIsNone(await self.dispatch(event("okkkk")))
        self.assertEqual(self.sent(), [])

    async def test_a_valid_answer_after_an_invalid_one_confirms(self):
        s = self.add(awaiting_answer=True)
        await self.dispatch(event("okkkk"))
        result = await self.dispatch(event("ok"))
        self.assertEqual(result["action"], "skip")
        self.assertEqual(self.stored(s).state, DONE)

    async def test_a_valid_answer_clears_the_flag(self):
        s = self.add(awaiting_answer=True, confirm={"kind": "word", "value": "later"})
        await self.dispatch(event("later 5"))
        self.assertFalse(self.stored(s).awaiting_answer)

    async def test_confirm_with_then_rewrites_without_extra_text(self):
        s = self.add(then="start the coffee machine", awaiting_answer=True)
        result = await self.dispatch(event("ok"))
        self.assertEqual(result["action"], "rewrite")
        self.assertNotIn("also wrote", result["text"])
        self.assertEqual(self.stored(s).state, DONE)

    async def test_invalid_reply_to_a_then_snooze_does_not_run_the_action(self):
        s = self.add(then="start the coffee machine")
        self.assertEqual(await self.dispatch(event("nope", reply_to="777")), hooks.SKIP)
        self.assertEqual(self.stored(s).state, ACTIVE)
        self.assertEqual(self.inject.calls, [])

    async def test_choice_list_has_no_ok_command(self):
        s = self.add(confirm={"kind": "choice", "value": "merge|review"}, awaiting_answer=True)
        await self.dispatch(event("maybe"))
        self.assertEqual(
            self.sent(),
            [
                (
                    "123",
                    f'"maybe" is not a valid answer for #{s.id} (Wake up).\n'
                    f"Reply: merge · review · 1 · 2 · later · later 15     Or type: /snooze cancel {s.id}",
                )
            ],
        )

    async def test_discord_list_says_reply_to_a_reminder(self):
        s = self.add(target=Target("discord", "chan", "42"), awaiting_answer=True)
        ev = event("okkkk", chat="chan", platform=Platform.DISCORD, chat_type="group")
        self.assertEqual(await self.dispatch(ev), hooks.SKIP)
        text = self.sent()[0][1]
        self.assertIn("Reply (to a reminder): Ok · later · later 15", text)
        self.assertIn(f"/snooze cancel {s.id}", text)

    async def test_stranger_in_the_chat_is_not_answered(self):
        s = self.add(awaiting_answer=True)
        self.assertIsNone(await self.dispatch(event("okkkk", user="7", reply_to="777")))
        self.assertTrue(self.stored(s).awaiting_answer)
        self.assertEqual(self.sent(), [])

    async def test_home_channel_snooze_is_left_to_post_gateway_admission(self):
        s = self.add(target=Target("discord", "chan", None), awaiting_answer=True)
        ev = event("okkkk", chat="chan", platform=Platform.DISCORD, chat_type="group")
        self.assertIsNone(await self.dispatch(ev))
        self.assertTrue(self.stored(s).awaiting_answer)
        self.assertEqual(self.sent(), [])

    async def test_pending_snooze_is_unchanged(self):
        s = self.add(state=PENDING, awaiting_answer=True)
        self.assertIsNone(await self.dispatch(event("hello")))
        self.assertEqual(self.stored(s).state, PENDING)

    async def test_reaction_does_not_touch_the_flag(self):
        s = self.add(confirm=EMOJI, awaiting_answer=True)
        self.hooks.gateway_platform_event(
            platform="telegram",
            event_type="reaction",
            payload={"emojis": ["👎"], "chat_id": "123", "message_id": "777"},
        )
        self.assertTrue(self.stored(s).awaiting_answer)


class NoUserIdTest(Base):
    """A snooze sent to a home channel has no user id: its answers come through post_gateway_admission."""

    PROMPT = ThenTest.PROMPT

    def add(self, **kw):
        return super().add(target=Target("discord", "chan", None), **kw)

    @staticmethod
    def source(chat="chan", user="42"):
        """The `SessionSource.to_dict()` shape Hermes passes."""
        return {"platform": "discord", "chat_id": chat, "chat_type": "group", "user_id": user, "thread_id": None}

    async def admit(self, text="ok", **kw):
        result = await self.hooks.post_gateway_admission(
            session_key="k", platform="discord", source=self.source(**kw), message_id="9", text=text
        )
        await asyncio.gather(*hooks._background_tasks)
        return result

    async def test_pre_gateway_dispatch_does_not_confirm_it(self):
        s = self.add()
        ev = event("ok", chat="chan", platform=Platform.DISCORD, chat_type="group")
        self.assertIsNone(await self.dispatch(ev))
        self.assertEqual(self.stored(s).state, ACTIVE)

    async def test_admitted_sender_confirms(self):
        s = self.add()
        self.assertEqual(await self.admit(), {"action": "handled"})
        self.assertEqual(self.stored(s).state, DONE)
        self.assertIsNone(self.stored(s).target.user_id)  # the record is not changed
        self.assertEqual(self.sent(), [("chan", "✅ Done: Wake up")])
        self.assertEqual(self.inject.calls, [])

    async def test_then_is_injected_in_the_senders_chat(self):
        s = self.add(then="start the coffee machine")
        self.assertEqual(await self.admit(), {"action": "handled"})
        self.assertEqual(self.stored(s).state, DONE)
        self.assertEqual(self.sent(), [("chan", "✅ Done: Wake up. Now: start the coffee machine…")])
        origin = {"platform": "discord", "chat_id": "chan", "chat_type": "group", "user_id": "42"}
        self.assertEqual(self.inject.calls, [(self.PROMPT, origin)])

    async def test_later_is_handled_without_then(self):
        s = self.add(then="start the coffee machine")
        self.assertEqual(await self.admit("later 30"), {"action": "handled"})
        self.assertEqual(self.stored(s).state, ACTIVE)
        self.assertEqual(self.inject.calls, [])

    async def test_invalid_first_reply_gets_the_list(self):
        s = self.add(awaiting_answer=True)
        self.assertEqual(await self.admit("okkkk"), {"action": "handled"})
        self.assertFalse(self.stored(s).awaiting_answer)
        self.assertIn("is not a valid answer", self.sent()[0][1])

    async def test_other_message_passes_on(self):
        s = self.add()
        self.assertIsNone(await self.admit("good morning"))
        self.assertIsNone(await self.admit("/snooze ok"))
        self.assertEqual(self.stored(s).state, ACTIVE)

    async def test_other_chat_passes_on(self):
        s = self.add()
        self.assertIsNone(await self.admit(chat="other"))
        self.assertEqual(self.stored(s).state, ACTIVE)

    async def test_sender_without_user_id_is_ignored(self):
        s = self.add()
        self.assertIsNone(await self.admit(user=None))
        self.assertEqual(self.stored(s).state, ACTIVE)

    async def test_snooze_with_an_owner_is_left_to_pre_gateway_dispatch(self):
        s = super().add(target=Target("discord", "chan", "42"))
        self.assertIsNone(await self.admit())
        self.assertEqual(self.stored(s).state, ACTIVE)

    async def test_owner_snooze_still_rewrites_next_to_an_owner_less_one(self):
        loose = self.add(text="Loose", last_sent_at=(NOW + timedelta(minutes=1)).isoformat())
        exact = super().add(target=Target("discord", "chan", "42"), then="start the coffee machine")
        ev = event("ok", chat="chan", platform=Platform.DISCORD, chat_type="group")
        self.assertEqual(await self.dispatch(ev), {"action": "rewrite", "text": self.PROMPT})
        self.assertEqual(self.stored(loose).state, ACTIVE)
        self.assertEqual(self.stored(exact).state, DONE)

    async def test_broken_store_lets_the_message_through(self):
        self.dir.mkdir()
        (self.dir / "snoozes.json").write_text("{not json")
        with self.assertLogs("snooze.hooks", "WARNING"):
            self.assertIsNone(await self.admit())


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
