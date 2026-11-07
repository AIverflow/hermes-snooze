import dataclasses
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

from snooze import commands, confirm
from snooze.commands import Commands
from snooze.store import (ACTIVE, CANCELLED, DONE, MISSED, PENDING, Schedule, Snooze, Store, Target)

NOW = datetime(2026, 11, 7, 10, 0, tzinfo=timezone.utc)  # 11:00 in Paris
HOME = Target(platform="telegram", chat_id="999", user_id="999")


class FakeCtx:
    """Plugin context stand-in: config values plus a record of what gets registered."""

    def __init__(self, **config):
        self.config = {"timezone": "Europe/Paris", **config}
        self.commands = {}
        self.tools = {}

    def get_config(self, key, default=None):
        return self.config.get(key, default)

    def register_command(self, name, handler, description="", args_hint="", argument_mode=None):
        self.commands[name] = handler

    def register_tool(self, name, toolset, schema, handler, **kwargs):
        self.tools[name] = (schema, handler)


def sent(snoozes):
    """The records as if a reminder had already gone out (confirm.match only answers snoozes that sent one)."""
    return [dataclasses.replace(s, last_sent_at=NOW.isoformat()) for s in snoozes]


def telegram_source(chat="123", user="42"):
    return SimpleNamespace(platform=SimpleNamespace(value="telegram"), chat_id=chat, user_id=user)


class Base(unittest.TestCase):
    config = {}

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name) / "data"
        self.now = NOW
        self.home = HOME
        self.env = {}
        self.ctx = FakeCtx(**self.config)
        self.cmds = Commands(
            self.ctx,
            data_dir=lambda: self.dir,
            now=lambda: self.now,
            home_target=lambda: self.home,
            session_env=lambda name: self.env.get(name, ""),
        )
        self.store = Store(self.dir)

    def run_cmd(self, text, source=None):
        return self.cmds.command(text, source=source)

    def create(self, text="Send email at 4pm every 10m", source=None):
        """Create through the command and return the stored record."""
        self.run_cmd(text, source=source or telegram_source())
        return self.store.load()[-1]

    def add(self, state=ACTIVE, **kw):
        fields = dict(
            text="Wake up",
            created=NOW.isoformat(),
            target=Target("telegram", "123", "42"),
            schedule=Schedule(first_at=(NOW + timedelta(minutes=5)).isoformat(), every=10, tz="Europe/Paris"),
            state=state,
        )
        fields.update(kw)
        return self.store.add(Snooze(**fields))


class CreateTest(Base):
    FULL = "Send email to John Doe at 4pm every 10m until 6pm then close ticket 42"

    def test_echo_content(self):
        echo = self.run_cmd(self.FULL, source=telegram_source())
        for part in (
            "Snooze #1", "Send email to John Doe", "today 16:00", "every 10 min", "stops 18:00",
            "(Europe/Paris)", 'reply "ok"', 'the agent will "close ticket 42"',
            "Telegram (this chat)", "/snooze cancel 1",
        ):
            self.assertIn(part, echo)

    def test_validation_echo_ends_with_yes_prompt(self):
        echo = self.run_cmd(self.FULL, source=telegram_source())
        self.assertTrue(echo.endswith("Reply `yes` to start it (or `/snooze yes 1`)."), echo)

    def test_new_snooze_is_pending_by_default_and_nothing_is_due(self):
        s = self.create(self.FULL)
        self.assertEqual(s.state, PENDING)
        self.assertIsNone(s.next_at)

    def test_stored_record(self):
        s = self.create(self.FULL)
        self.assertEqual(s.text, "Send email to John Doe")
        self.assertEqual(s.then, "close ticket 42")
        self.assertEqual(s.target, Target("telegram", "123", "42"))
        self.assertEqual(s.schedule.first_at, "2026-11-07T15:00:00+00:00")
        self.assertEqual(s.schedule.until, "2026-11-07T17:00:00+00:00")
        self.assertEqual(s.schedule.tz, "Europe/Paris")
        self.assertEqual(s.created, NOW.isoformat())

    def test_every_is_stored_in_minutes(self):
        self.assertEqual(self.create("Wake up at 4pm every 10m").schedule.every, 10)
        self.assertEqual(self.create("Wake up at 4pm every 1h30m").schedule.every, 90)
        self.assertEqual(self.create("Wake up at 4pm every 90s").schedule.every, 1.5)

    def test_default_every_when_not_given(self):
        s = self.create("Wake up at 4pm")
        self.assertEqual(s.schedule.every, commands.DEFAULT_EVERY.total_seconds() / 60)

    def test_cap_when_no_until(self):
        s = self.create("Wake up at 4pm every 10m")
        self.assertEqual(s.schedule.until, "2026-11-07T17:00:00+00:00")  # 15:00Z + 120 min
        echo = self.run_cmd("Wake up at 4pm every 10m", source=telegram_source())
        self.assertIn("stops 18:00 or after 10 reminders", echo)

    def test_explicit_until_is_not_capped(self):
        echo = self.run_cmd("Wake up at 4pm every 10m until 11pm", source=telegram_source())
        self.assertIn("stops 23:00", echo)
        self.assertNotIn("reminders", echo)

    def test_tomorrow_label(self):
        echo = self.run_cmd("Wake up 8am 10m", source=telegram_source())
        self.assertIn("tomorrow 08:00", echo)

    def test_stop_hint_follows_confirm_rule(self):
        cases = {
            "ok taken": 'reply "taken"',
            "ok any": "any message",
            "ok contains:sent": 'containing "sent"',
            "ok 👍": "send 👍",
        }
        for suffix, expected in cases.items():
            with self.subTest(suffix=suffix):
                echo = self.run_cmd(f"Do it in 30m every 5m {suffix}", source=telegram_source())
                self.assertIn(expected, echo)

    def test_parse_error_sets_nothing(self):
        reply = self.run_cmd("Wake up", source=telegram_source())
        self.assertIn("Missing when to start", reply)
        self.assertIn("Example: /snooze", reply)
        self.assertIn("Nothing was set", reply)
        self.assertEqual(self.store.load(), [])

    def test_empty_shows_help(self):
        self.assertIn("Usage: /snooze", self.run_cmd(""))
        self.assertEqual(self.store.load(), [])

    def test_ids_increase(self):
        self.create()
        self.create()
        self.assertEqual([s.id for s in self.store.load()], [1, 2])

    def test_hermes_style_extra_keywords_are_accepted(self):
        reply = self.cmds.command("list", source=None, session_id="s", session_key="k", platform="cli")
        self.assertEqual(reply, "No snoozes running.")


class ValidateSetupOffTest(Base):
    config = {"validate_setup": False}

    def test_active_at_once(self):
        s = self.create("Wake up at 4pm every 10m")
        self.assertEqual(s.state, ACTIVE)
        self.assertEqual(s.next_at, s.schedule.first_at)

    def test_echo_has_no_yes_prompt(self):
        echo = self.run_cmd("Wake up at 4pm every 10m", source=telegram_source())
        self.assertNotIn("`yes`", echo)
        self.assertIn("Snooze #1 set", echo)
        self.assertIn("Undo:     /snooze cancel 1", echo)

    def test_string_false_is_false(self):
        self.ctx.config["validate_setup"] = "false"
        self.assertEqual(self.create().state, ACTIVE)


class TargetTest(Base):
    def test_gateway_source_is_the_target(self):
        s = self.create(source=telegram_source(chat="-100777", user="42"))
        self.assertEqual(s.target, Target("telegram", "-100777", "42"))

    def test_any_gateway_platform(self):
        source = SimpleNamespace(platform=SimpleNamespace(value="discord"), chat_id=555, user_id=7)
        s = self.create(source=source)
        self.assertEqual(s.target, Target("discord", "555", "7"))

    def test_plain_string_platform_works(self):
        source = SimpleNamespace(platform="whatsapp", chat_id="a@b", user_id=None)
        self.assertEqual(self.create(source=source).target, Target("whatsapp", "a@b", None))

    def test_cli_uses_home_channel(self):
        self.run_cmd("Wake up at 4pm every 10m", source=None)
        s = self.store.load()[0]
        self.assertEqual(s.target, HOME)

    def test_echo_says_home_channel(self):
        echo = self.run_cmd("Wake up at 4pm every 10m", source=None)
        self.assertIn("Telegram (home channel)", echo)

    def test_non_chat_platform_uses_home_channel(self):
        source = SimpleNamespace(platform=SimpleNamespace(value="api_server"), chat_id="x", user_id="y")
        self.assertEqual(self.create(source=source).target, HOME)

    def test_no_chat_and_no_home_sets_nothing(self):
        self.home = None
        reply = self.run_cmd("Wake up at 4pm every 10m", source=None)
        self.assertIn("No chat to send reminders to", reply)
        self.assertEqual(self.store.load(), [])


class ConfirmRoundTripTest(Base):
    """A stored rule is read back by confirm.match; `every` is in minutes."""

    config = {"validate_setup": False}
    ME = {"platform": "telegram", "chat_id": "123", "user_id": "42"}

    def decide(self, message):
        return confirm.match(message, sent(self.store.load()), self.ME)

    def test_default_ok(self):
        s = self.create("Wake up in 5m every 5m")
        self.assertEqual(s.confirm, {"kind": "ok", "value": ""})
        self.assertEqual(self.decide("ok").action, confirm.CONFIRM)
        self.assertEqual(self.decide("taken").action, confirm.NONE)

    def test_word(self):
        s = self.create("Take my pill in 30m every 5m ok taken")
        self.assertEqual(s.confirm, {"kind": "word", "value": "taken"})
        self.assertEqual(self.decide("Taken!").action, confirm.CONFIRM)
        self.assertEqual(self.decide("ok").action, confirm.NONE)

    def test_contains(self):
        self.create("Report in 30m every 5m ok contains:sent")
        self.assertEqual(self.decide("I sent it").action, confirm.CONFIRM)

    def test_any(self):
        self.create("Report in 30m every 5m ok any")
        self.assertEqual(self.decide("whatever").action, confirm.CONFIRM)

    def test_emoji(self):
        self.create("Report in 30m every 5m ok 👍")
        self.assertEqual(self.decide("👍").action, confirm.CONFIRM)

    def test_later_default_is_the_stored_interval_in_minutes(self):
        self.create("Wake up in 5m every 7m")
        decision = self.decide("later")
        self.assertEqual((decision.action, decision.later_minutes), (confirm.LATER, 7))

    def test_later_default_with_hours(self):
        self.create("Wake up in 5m every 1h30m")
        self.assertEqual(self.decide("later").later_minutes, 90)

    def test_later_default_when_every_not_given(self):
        self.create("Wake up in 5m")
        self.assertEqual(self.decide("later").later_minutes, 10)

    def test_yes_matches_a_pending_snooze(self):
        self.ctx.config["validate_setup"] = True
        s = self.create("Wake up in 5m every 5m")
        decision = self.decide("yes")
        self.assertEqual((decision.action, decision.snooze_id), (confirm.YES, s.id))


class ListTest(Base):
    def test_empty(self):
        self.assertEqual(self.run_cmd("list"), "No snoozes running.")

    def test_shows_active_and_pending_only(self):
        self.add(text="Active one", next_at=(NOW + timedelta(minutes=5)).isoformat(), sent_count=2)
        self.add(state=PENDING, text="Waiting one")
        self.add(state=DONE, text="Done one")
        self.add(state=CANCELLED, text="Cancelled one")
        reply = self.run_cmd("list")
        self.assertIn("#1 Active one — next today 11:05, 2 sent", reply)
        self.assertIn("#2 Waiting one — waiting for yes, until today 11:10", reply)
        self.assertNotIn("Done one", reply)
        self.assertNotIn("Cancelled one", reply)

    def test_expired_pending_is_flagged(self):
        self.add(state=PENDING, created=(NOW - timedelta(minutes=11)).isoformat())
        self.assertIn("expired", self.run_cmd("list"))

    def test_last_error_is_shown(self):
        self.add(last_error="chat not found", next_at=NOW.isoformat())
        self.assertIn("last error: chat not found", self.run_cmd("list"))


class OkTest(Base):
    def test_ok_marks_done(self):
        self.add(next_at=NOW.isoformat())
        self.assertEqual(self.run_cmd("ok"), "✅ Done: Wake up")
        s = self.store.load()[0]
        self.assertEqual((s.state, s.next_at), (DONE, None))

    def test_ok_with_id(self):
        self.add(text="One")
        self.add(text="Two")
        self.assertEqual(self.run_cmd("ok #1"), "✅ Done: One")
        self.assertEqual([s.state for s in self.store.load()], [DONE, ACTIVE])

    def test_ok_picks_the_snooze_that_sent_last(self):
        self.add(text="Old", last_sent_at="2026-11-07T09:00:00+00:00")
        self.add(text="Recent", last_sent_at="2026-11-07T09:50:00+00:00")
        self.add(text="Never sent")
        self.assertEqual(self.run_cmd("ok"), "✅ Done: Recent")

    def test_ok_nothing_running(self):
        self.assertEqual(self.run_cmd("ok"), "No running snooze.")

    def test_ok_unknown_id(self):
        self.assertEqual(self.run_cmd("ok 9"), "No snooze #9.")

    def test_ok_on_pending_points_to_yes(self):
        self.add(state=PENDING)
        reply = self.run_cmd("ok 1")
        self.assertIn("has not started", reply)
        self.assertIn("`yes`", reply)
        self.assertEqual(self.store.load()[0].state, PENDING)

    def test_ok_on_finished(self):
        self.add(state=DONE)
        self.assertEqual(self.run_cmd("ok 1"), "Snooze #1 is already done.")


class LaterTest(Base):
    def test_later_minutes(self):
        self.add()
        reply = self.run_cmd("later 30")
        self.assertEqual(reply, "⏰ OK, back at 11:30: Wake up")
        self.assertEqual(self.store.load()[0].next_at, "2026-11-07T10:30:00+00:00")

    def test_later_with_id(self):
        self.add(text="One")
        self.add(text="Two")
        self.run_cmd("later 5 1")
        self.assertEqual(self.store.load()[0].next_at, "2026-11-07T10:05:00+00:00")
        self.assertIsNone(self.store.load()[1].next_at)

    def test_later_duration_forms(self):
        self.add()
        self.run_cmd("later 1h")
        self.assertEqual(self.store.load()[0].next_at, "2026-11-07T11:00:00+00:00")
        self.run_cmd("later 15m")
        self.assertEqual(self.store.load()[0].next_at, "2026-11-07T10:15:00+00:00")

    def test_later_without_number_uses_the_interval(self):
        self.add(schedule=Schedule(first_at=NOW.isoformat(), every=7, tz="Europe/Paris"))
        self.run_cmd("later")
        self.assertEqual(self.store.load()[0].next_at, "2026-11-07T10:07:00+00:00")

    def test_later_keeps_the_snooze_active(self):
        self.add()
        self.run_cmd("later 30")
        self.assertEqual(self.store.load()[0].state, ACTIVE)

    def test_later_on_pending(self):
        self.add(state=PENDING)
        self.assertIn("has not started", self.run_cmd("later 5 1"))

    def test_later_bad_number_shows_usage(self):
        self.add()
        for text in ("later 0", "later soon", "later 5 x", "later 5 1 2"):
            with self.subTest(text=text):
                self.assertIn("Usage: /snooze later", self.run_cmd(text))
        self.assertIsNone(self.store.load()[0].next_at)


class CancelTest(Base):
    def test_cancel_active(self):
        self.add(next_at=NOW.isoformat())
        self.assertEqual(self.run_cmd("cancel 1"), "🛑 Cancelled #1: Wake up")
        s = self.store.load()[0]
        self.assertEqual((s.state, s.next_at), (CANCELLED, None))

    def test_cancel_pending(self):
        self.add(state=PENDING)
        self.run_cmd("cancel #1")
        self.assertEqual(self.store.load()[0].state, CANCELLED)

    def test_cancel_unknown(self):
        self.assertEqual(self.run_cmd("cancel 4"), "No snooze #4.")

    def test_cancel_twice(self):
        self.add(state=CANCELLED)
        self.assertEqual(self.run_cmd("cancel 1"), "Snooze #1 is already cancelled.")

    def test_cancel_needs_an_id(self):
        self.assertIn("Usage: /snooze cancel", self.run_cmd("cancel"))


class YesTest(Base):
    def test_yes_starts_the_newest_pending(self):
        self.add(state=PENDING, text="Older")
        self.add(state=PENDING, text="Newer")
        reply = self.run_cmd("yes")
        self.assertTrue(reply.startswith("▶️ Started #2: Newer"), reply)
        self.assertEqual([s.state for s in self.store.load()], [PENDING, ACTIVE])

    def test_yes_makes_the_reminders_start(self):
        first = (NOW + timedelta(minutes=5)).isoformat()
        self.add(state=PENDING, schedule=Schedule(first_at=first, every=1, tz="Europe/Paris"))
        self.run_cmd("yes")
        s = self.store.load()[0]
        self.assertEqual((s.state, s.next_at), (ACTIVE, first))

    def test_yes_with_id(self):
        self.add(state=PENDING)
        self.add(state=PENDING)
        self.run_cmd("yes 1")
        self.assertEqual([s.state for s in self.store.load()], [ACTIVE, PENDING])

    def test_yes_without_pending(self):
        self.assertEqual(self.run_cmd("yes"), "No snooze waiting for yes.")

    def test_yes_on_active(self):
        self.add()
        self.assertEqual(self.run_cmd("yes 1"), "Snooze #1 is already running.")

    def test_yes_after_the_window_does_not_start(self):
        self.add(state=PENDING, text="Late one", created=(NOW - timedelta(minutes=10)).isoformat())
        self.assertEqual(self.run_cmd("yes"), "⌛ Not started: Late one")
        s = self.store.load()[0]
        self.assertEqual((s.state, s.next_at), (MISSED, None))

    def test_yes_just_inside_the_window(self):
        self.add(state=PENDING, created=(NOW - timedelta(minutes=9, seconds=59)).isoformat())
        self.assertIn("Started #1", self.run_cmd("yes"))

    def test_full_flow_from_create_to_yes(self):
        self.run_cmd("Test in 1m every 1m", source=telegram_source())
        self.assertEqual(self.store.load()[0].state, PENDING)
        self.now = NOW + timedelta(minutes=2)
        self.assertIn("Started #1", self.run_cmd("yes"))
        s = self.store.load()[0]
        self.assertEqual((s.state, s.schedule.every), (ACTIVE, 1))
        self.assertEqual(s.next_at, "2026-11-07T10:01:00+00:00")


class TestStubTest(Base):
    def test_not_available_yet(self):
        self.assertIn("not available yet", self.run_cmd("test"))


class FallThroughTest(Base):
    def test_subcommand_word_starting_a_reminder(self):
        s = self.create("cancel the order at 4pm every 10m")
        self.assertEqual(s.text, "cancel the order")

    def test_yes_word_starting_a_reminder(self):
        s = self.create("yes we can at 4pm")
        self.assertEqual(s.text, "yes we can")

    def test_bad_shape_that_is_not_a_reminder_shows_usage(self):
        self.assertIn("Usage: /snooze list", self.run_cmd("list everything"))
        self.assertIn("Usage: /snooze ok", self.run_cmd("ok 1 2"))
        self.assertEqual(self.store.load(), [])

    def test_keywords_are_case_insensitive(self):
        self.add()
        self.assertEqual(self.run_cmd("OK"), "✅ Done: Wake up")


class PendingExpiryTest(unittest.TestCase):
    def snooze(self, state=PENDING, created=NOW):
        return Snooze(text="x", created=created.isoformat(), target=Target("telegram", "1", "1"),
                      schedule=Schedule(first_at=NOW.isoformat()), state=state)

    def test_deadline_is_created_plus_ten_minutes(self):
        self.assertEqual(commands.pending_deadline(self.snooze()), NOW + timedelta(minutes=10))

    def test_not_expired_before_the_deadline(self):
        s = self.snooze()
        self.assertFalse(commands.is_pending_expired(s, NOW))
        self.assertFalse(commands.is_pending_expired(s, NOW + timedelta(minutes=9, seconds=59)))

    def test_expired_at_the_deadline(self):
        self.assertTrue(commands.is_pending_expired(self.snooze(), NOW + timedelta(minutes=10)))

    def test_only_pending_snoozes_expire(self):
        late = NOW + timedelta(hours=5)
        for state in (ACTIVE, DONE, MISSED, CANCELLED):
            with self.subTest(state=state):
                self.assertFalse(commands.is_pending_expired(self.snooze(state=state), late))

    def test_unreadable_created_never_expires(self):
        s = self.snooze()
        s.created = "garbage"
        self.assertIsNone(commands.pending_deadline(s))
        self.assertFalse(commands.is_pending_expired(s, NOW + timedelta(days=1)))

    def test_not_started_message(self):
        self.assertEqual(commands.not_started_message(self.snooze()), "⌛ Not started: x")


class ToolTest(Base):
    def call(self, **args):
        return json.loads(self.cmds.tool(args))

    def setUp(self):
        super().setUp()
        self.env = {"HERMES_SESSION_PLATFORM": "telegram", "HERMES_SESSION_CHAT_ID": "123",
                    "HERMES_SESSION_USER_ID": "42"}

    def test_creates_a_pending_snooze_with_echo(self):
        result = self.call(text="Send email to John Doe", first_at="2026-11-07T16:00", every="10m",
                           until="2026-11-07T18:00", then="close ticket 42")
        self.assertTrue(result["ok"])
        self.assertEqual((result["id"], result["state"]), (1, PENDING))
        self.assertIn("today 16:00", result["message"])
        self.assertIn("stops 18:00", result["message"])
        self.assertTrue(result["message"].endswith("Reply `yes` to start it (or `/snooze yes 1`)."))
        self.assertIn("NOT started", result["note"])
        s = self.store.load()[0]
        self.assertEqual(s.schedule.first_at, "2026-11-07T15:00:00+00:00")
        self.assertEqual(s.schedule.until, "2026-11-07T17:00:00+00:00")
        self.assertEqual(s.schedule.every, 10)
        self.assertEqual(s.then, "close ticket 42")
        self.assertEqual(s.target, Target("telegram", "123", "42"))

    def test_validate_setup_off_starts_at_once(self):
        self.ctx.config["validate_setup"] = False
        result = self.call(text="Wake up", first_at="2026-11-07T16:00")
        self.assertEqual(result["state"], ACTIVE)
        self.assertNotIn("`yes`", result["message"])
        s = self.store.load()[0]
        self.assertEqual(s.next_at, s.schedule.first_at)

    def test_defaults_match_the_command(self):
        self.call(text="Wake up", first_at="2026-11-07T16:00")
        s = self.store.load()[0]
        self.assertEqual(s.schedule.every, 10)
        self.assertEqual(s.schedule.until, "2026-11-07T17:00:00+00:00")
        self.assertEqual(s.confirm, {"kind": "ok", "value": ""})

    def test_every_forms(self):
        for value, expected in ((5, 5), ("5", 5), ("1h", 60), ("90s", 1.5), (2.5, 2.5)):
            with self.subTest(value=value):
                self.call(text="Wake up", first_at="2026-11-07T16:00", every=value)
                self.assertEqual(self.store.load()[-1].schedule.every, expected)

    def test_offset_in_first_at_is_respected(self):
        self.call(text="Wake up", first_at="2026-11-07T16:00:00+00:00")
        self.assertEqual(self.store.load()[0].schedule.first_at, "2026-11-07T16:00:00+00:00")

    def test_confirm_values_round_trip_through_match(self):
        self.ctx.config["validate_setup"] = False
        me = {"platform": "telegram", "chat_id": "123", "user_id": "42"}
        for value, said in (("taken", "taken"), ("contains:sent", "I sent it"), ("any", "hmm"), ("👍", "👍"), ("ok", "ok")):
            with self.subTest(value=value):
                self.call(text="Wake up", first_at="2026-11-07T16:00", confirm=value)
                last = sent(self.store.load()[-1:])
                self.assertEqual(confirm.match(said, last, me).action, confirm.CONFIRM)

    def test_confirm_dict(self):
        self.call(text="Wake up", first_at="2026-11-07T16:00", confirm={"kind": "word", "value": "done"})
        self.assertEqual(self.store.load()[0].confirm, {"kind": "word", "value": "done"})

    def test_errors_set_nothing(self):
        cases = {
            "no text": dict(first_at="2026-11-07T16:00"),
            "no first_at": dict(text="x"),
            "bad first_at": dict(text="x", first_at="tomorrow at 4"),
            "past": dict(text="x", first_at="2026-11-07T09:00"),
            "until before first": dict(text="x", first_at="2026-11-07T16:00", until="2026-11-07T15:00"),
            "bad until": dict(text="x", first_at="2026-11-07T16:00", until="later"),
            "every too short": dict(text="x", first_at="2026-11-07T16:00", every="30s"),
            "every zero": dict(text="x", first_at="2026-11-07T16:00", every=0),
            "every junk": dict(text="x", first_at="2026-11-07T16:00", every="often"),
            "confirm two words": dict(text="x", first_at="2026-11-07T16:00", confirm="all good"),
            "confirm bad kind": dict(text="x", first_at="2026-11-07T16:00", confirm={"kind": "nope"}),
        }
        for name, args in cases.items():
            with self.subTest(name):
                result = self.call(**args)
                self.assertIn("error", result)
                self.assertNotIn("ok", result)
        self.assertEqual(self.store.load(), [])

    def test_cli_session_uses_home_channel(self):
        self.env = {"HERMES_SESSION_PLATFORM": "cli"}
        self.call(text="Wake up", first_at="2026-11-07T16:00")
        self.assertEqual(self.store.load()[0].target, HOME)

    def test_no_session_and_no_home(self):
        self.env = {}
        self.home = None
        result = self.call(text="Wake up", first_at="2026-11-07T16:00")
        self.assertIn("No chat to send reminders to", result["error"])

    def test_non_dict_args(self):
        self.assertIn("error", json.loads(self.cmds.tool(None)))

    def test_returns_valid_json_string(self):
        self.assertIsInstance(self.cmds.tool({"text": "x", "first_at": "2026-11-07T16:00"}), str)


class SettingsTest(unittest.TestCase):
    def test_defaults_without_get_config(self):
        settings = commands.read_settings(object())
        self.assertEqual((settings.validate_setup, settings.cap_minutes, settings.cap_reminders), (True, 120, 10))

    def test_values_are_read(self):
        ctx = FakeCtx(validate_setup=False, cap_minutes=30, cap_reminders=3, timezone="Asia/Tokyo")
        settings = commands.read_settings(ctx)
        self.assertEqual((settings.validate_setup, settings.cap_minutes, settings.cap_reminders, settings.timezone),
                         (False, 30, 3, "Asia/Tokyo"))

    def test_invalid_values_fall_back(self):
        ctx = FakeCtx(validate_setup="maybe", cap_minutes=-5, cap_reminders="lots", timezone="Mars/Base")
        settings = commands.read_settings(ctx)
        self.assertEqual((settings.validate_setup, settings.cap_minutes, settings.cap_reminders), (True, 120, 10))
        self.assertEqual(settings.timezone, commands.system_timezone())

    def test_get_config_that_raises(self):
        class Broken:
            def get_config(self, key, default=None):
                raise RuntimeError("boom")

        self.assertEqual(commands.read_settings(Broken()).cap_minutes, 120)

    def test_system_timezone_is_a_real_zone(self):
        name = commands.resolve_timezone("system")
        self.assertTrue(commands._valid_zone(name))
        self.assertEqual(commands.resolve_timezone(""), name)

    def test_caps_apply_to_new_snoozes(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        ctx = FakeCtx(cap_minutes=30, cap_reminders=3)
        cmds = Commands(ctx, data_dir=lambda: tmp.name, now=lambda: NOW, home_target=lambda: HOME)
        echo = cmds.command("Wake up at 4pm every 10m", source=telegram_source())
        self.assertIn("stops 16:30 or after 3 reminders", echo)


class RegisterTest(unittest.TestCase):
    def test_registers_command_and_tool(self):
        ctx = FakeCtx()
        commands.register_all(ctx)
        self.assertEqual(list(ctx.commands), ["snooze"])
        schema, handler = ctx.tools["snooze_create"]
        self.assertEqual(schema["name"], "snooze_create")
        self.assertEqual(schema["parameters"]["required"], ["text", "first_at"])
        self.assertTrue(callable(handler))

    def test_tool_fields_follow_the_parser_output(self):
        fields = set(commands.TOOL_SCHEMA["parameters"]["properties"])
        self.assertEqual(fields, {"text", "first_at", "every", "until", "confirm", "then"})


class FormatTest(unittest.TestCase):
    def test_format_minutes(self):
        self.assertEqual(commands.format_minutes(10), "10 min")
        self.assertEqual(commands.format_minutes(60), "1 h")
        self.assertEqual(commands.format_minutes(90), "1 h 30 min")
        self.assertEqual(commands.format_minutes(1.5), "1 min 30 s")

    def test_format_when_labels(self):
        zone = "Europe/Paris"
        self.assertEqual(commands.format_when(NOW, zone, NOW), "today 11:00")
        self.assertEqual(commands.format_when(NOW + timedelta(days=1), zone, NOW), "tomorrow 11:00")
        self.assertEqual(commands.format_when(NOW + timedelta(days=3), zone, NOW), "Tue 10 Nov 11:00")
        self.assertEqual(commands.format_when(NOW + timedelta(hours=1), zone, NOW, relative_to=NOW), "12:00")

    def test_local_day_not_utc_day(self):
        late = datetime(2026, 11, 7, 23, 30, tzinfo=timezone.utc)  # 00:30 on the 8th in Paris
        self.assertEqual(commands.format_when(late, "Europe/Paris", NOW), "tomorrow 00:30")


if __name__ == "__main__":
    unittest.main()
