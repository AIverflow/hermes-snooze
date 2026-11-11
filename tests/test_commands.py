import dataclasses
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from snooze import commands, confirm, echo, parse, schedule
from snooze.commands import Commands
from snooze.store import ACTIVE, CANCELLED, DONE, MISSED, PENDING, Schedule, Snooze, Store, Target

NOW = datetime(2026, 11, 7, 10, 0, tzinfo=timezone.utc)  # 11:00 in Paris
HOME = Target(platform="telegram", chat_id="999", user_id="999")
DISCORD_HOME = Target(platform="discord", chat_id="channel-1", user_id=None)


class FakeCtx:
    """Plugin context stand-in: config values plus a record of what gets registered."""

    def __init__(self, **config):
        self.config = {"timezone": "Europe/Paris", **config}
        self.commands = {}
        self.tools = {}

    def get_config(self, key, default=None):
        return self.config.get(key, default)

    def set_config(self, key, value):
        self.config[key] = value

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
        self.homes = {"discord": DISCORD_HOME, "telegram": HOME}  # per platform, for `via`
        self.platforms = ["telegram"]  # the platforms that have a home channel: one, unless a test says otherwise
        self.env = {}
        self.paused = False
        self.gateway = True  # True, False or None (cannot tell)
        self.ctx = FakeCtx(**self.config)
        self.cmds = Commands(
            self.ctx,
            data_dir=lambda: self.dir,
            now=lambda: self.now,
            home_target=lambda platform=None: self.homes.get(platform) if platform else self.home,
            home_platforms=lambda: self.platforms,
            session_env=lambda name: self.env.get(name, ""),
            is_paused=lambda: self.paused,
            gateway_running=lambda: self.gateway,
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
            "Snooze #1",
            "Send email to John Doe",
            "today 16:00",
            "every 10 min",
            "stops 18:00",
            "(Europe/Paris)",
            'reply "Ok"',
            'the agent will "close ticket 42"',
            "Telegram (this chat)",
            "/snooze cancel 1",
        ):
            self.assertIn(part, echo)

    def test_new_snooze_starts_at_once_by_default(self):
        s = self.create(self.FULL)
        self.assertEqual(s.state, ACTIVE)
        self.assertEqual(s.next_at, s.schedule.first_at)

    def test_recurring_creation_stores_days_and_span_and_summarizes_it(self):
        reply = self.run_cmd("Take my pill at 8am daily", source=telegram_source())
        s = self.store.load()[0]
        self.assertEqual(s.schedule.days, list(range(7)))
        self.assertEqual(s.schedule.span, 120)
        self.assertIn("daily at 8:00", reply)

    def test_every_day_is_weekly_and_on_day_is_one_time(self):
        weekly = self.run_cmd("Water the plants at 8am every monday", source=telegram_source())
        once = self.run_cmd("Call the dentist on monday at 9am", source=telegram_source())
        weekly_snooze, once_snooze = self.store.load()
        self.assertEqual(weekly_snooze.schedule.days, [0])
        self.assertIsNone(once_snooze.schedule.days)
        self.assertIn("Mon at 8:00", weekly)
        self.assertIn("Mon 9 Nov 09:00", once)

    def test_started_echo_has_no_yes_line_and_ends_with_undo(self):
        echo = self.run_cmd(self.FULL, source=telegram_source())
        self.assertNotIn("`yes`", echo)
        self.assertEqual(echo.splitlines()[-1], "Undo:     /snooze cancel 1")


class ValidateSetupOnTest(Base):
    config = {"validate_setup": True}
    FULL = CreateTest.FULL

    def test_new_snooze_is_pending_and_nothing_is_due(self):
        s = self.create(self.FULL)
        self.assertEqual(s.state, PENDING)
        self.assertIsNone(s.next_at)

    def test_chat_echo_ends_with_yes_line(self):
        echo = self.run_cmd(self.FULL, source=telegram_source())
        self.assertEqual(echo.splitlines()[-2], "Undo:     /snooze cancel 1")
        self.assertEqual(echo.splitlines()[-1], "Waiting:  not started. Reply `yes` within 10 min.")

    def test_cli_echo_ends_with_type_yes_line(self):
        echo = self.run_cmd(self.FULL, source=None)
        self.assertEqual(
            echo.splitlines()[-1],
            "Waiting:  not started. Type `/snooze yes 1` within 10 min (a bare `yes` only works in chat apps).",
        )

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

    def test_explicit_until_keeps_its_time_and_shows_the_reminder_cap(self):
        echo = self.run_cmd("Wake up at 4pm every 10m until 11pm", source=telegram_source())
        self.assertIn("stops 23:00 or after 10 reminders", echo)

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

    def test_choice_echo_lists_answer_options(self):
        echo = self.run_cmd("Dinner? in 30m ok asian|french|italian", source=telegram_source())
        self.assertIn("reply with one of: 1 asian | 2 french | 3 italian", echo)

    def test_parse_error_sets_nothing(self):
        reply = self.run_cmd("Wake up", source=telegram_source())
        self.assertIn("Missing when to start", reply)
        self.assertIn("Example: /snooze", reply)
        self.assertIn("Nothing was set", reply)
        self.assertEqual(self.store.load(), [])

    def test_empty_shows_help(self):
        self.assertEqual(self.run_cmd(""), commands.HELP)
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


class ParseErrorReplyTest(Base):
    """Rejected `/snooze` text names the clause in fault and creates no snooze."""

    def assertRefused(self, text, *fragments):
        reply = self.run_cmd(text, source=telegram_source())
        self.assertIn("Nothing was set", reply)
        self.assertIn("Example: /snooze", reply)
        for fragment in fragments:
            self.assertIn(fragment, reply)
        self.assertEqual(self.store.load(), [])
        return reply

    def test_trailing_word_after_a_clause(self):
        reply = self.assertRefused("Send report at 16:00 every 10m please", "`please`")
        self.assertNotIn("Missing when to start", reply)

    def test_spoken_duration(self):
        self.assertRefused("Send report at 16:00 every ten minutes", "`every ten minutes`")

    def test_unknown_unit(self):
        self.assertRefused("Send report at 16:00 every 10x", "`every 10x`")

    def test_contradictory_starts(self):
        self.assertRefused("Send report at 16:00 in 30m", "Two start times")

    def test_legit_sentences_still_create(self):
        record = self.create("Call mom before 6pm at 4pm")
        self.assertEqual(record.text, "Call mom before 6pm")


class HelpTest(Base):
    EXAMPLES = (commands.EXAMPLE_SIMPLE, commands.EXAMPLE_ACTION, commands.EXAMPLE_CHOICE)

    def test_help_shows_the_three_levels_with_their_replies(self):
        text = self.run_cmd("")
        for example in self.EXAMPLES:
            self.assertIn(example, text)
        self.assertIn("→ reply: Ok | later", text)
        self.assertIn("→ reply: merge | review | later", text)

    def test_help_ends_with_the_user_guide_line_and_has_no_github_link(self):
        lines = self.run_cmd("").splitlines()
        self.assertEqual(lines[-1], "More keywords and examples: see the user guide (docs/user-guide.md).")
        self.assertTrue(lines[-2].startswith("Other commands:"))
        self.assertNotIn("github.com", commands.HELP)

    def test_help_examples_parse(self):
        for example in self.EXAMPLES:
            with self.subTest(example=example):
                spec = parse.parse(example.removeprefix("/snooze "), NOW, "UTC")
                self.assertNotIsInstance(spec, parse.ParseError)

    def test_simple_and_action_replies_are_true(self):
        sender = confirm.Sender("telegram", "123", "42")
        for example in (commands.EXAMPLE_SIMPLE, commands.EXAMPLE_ACTION):
            with self.subTest(example=example):
                snooze = self.create(example.removeprefix("/snooze "))
                self.assertEqual(confirm.match("Ok", sent([snooze]), sender).action, confirm.CONFIRM)
                self.assertEqual(confirm.match("later", sent([snooze]), sender).action, confirm.LATER)

    def test_choice_replies_are_true(self):
        snooze = self.create(commands.EXAMPLE_CHOICE.removeprefix("/snooze "))
        sender = confirm.Sender("telegram", "123", "42")
        for reply in ("merge", "1", "review", "2", "later"):
            with self.subTest(reply=reply):
                self.assertNotEqual(confirm.match(reply, sent([snooze]), sender).action, confirm.NONE)


class OkCommandTest(Base):
    def test_choice_requires_answer_in_chat_and_stays_active(self):
        snooze = self.add(confirm={"kind": "choice", "value": "asian|french|italian"})
        reply = self.run_cmd(f"ok {snooze.id}")
        self.assertEqual(reply, f"#{snooze.id} needs an answer: reply 1 asian | 2 french | 3 italian in the chat.")
        self.assertEqual(self.store.get(snooze.id).state, ACTIVE)
        self.assertIsNone(self.store.get(snooze.id).answer)

    def test_plain_snooze_still_confirms(self):
        snooze = self.add()
        self.assertEqual(self.run_cmd(f"ok {snooze.id}"), "✅ Done: Wake up")
        self.assertEqual(self.store.get(snooze.id).state, DONE)


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


class ViaTest(Base):
    def test_command_via_sends_to_that_platforms_home_channel(self):
        echo = self.run_cmd("Water the plants at 8am daily via discord", source=telegram_source())
        s = self.store.load()[0]
        self.assertEqual(s.target, DISCORD_HOME)
        self.assertEqual(s.text, "Water the plants")
        self.assertEqual(s.schedule.days, list(range(7)))
        self.assertIn("Discord (home channel)", echo)

    def test_via_beats_the_notification_platform_setting(self):
        self.ctx.config["notification_platform"] = "telegram"
        self.run_cmd("Wake up at 4pm via discord", source=telegram_source())
        self.assertEqual(self.store.load()[0].target, DISCORD_HOME)

    def test_creators_user_id_is_never_copied_to_the_target(self):
        self.run_cmd("Wake up at 4pm via discord", source=telegram_source(user="42"))
        self.assertIsNone(self.store.load()[0].target.user_id)

    def test_home_channels_own_user_id_is_kept(self):
        self.homes["discord"] = Target("discord", "channel-1", "owner-7")
        self.run_cmd("Wake up at 4pm via discord", source=telegram_source(user="42"))
        self.assertEqual(self.store.load()[0].target.user_id, "owner-7")

    def test_via_without_home_channel_is_refused_and_nothing_is_stored(self):
        self.homes = {}
        reply = self.run_cmd("Wake up at 4pm via discord", source=telegram_source())
        self.assertIn("No Discord home channel", reply)
        self.assertIn("/sethome", reply)
        self.assertIn("Nothing was set", reply)
        self.assertEqual(self.store.load(), [])

    def test_unknown_platform_stays_in_the_text(self):
        self.run_cmd("Go to Lyon via Dijon at 4pm", source=telegram_source())
        s = self.store.load()[0]
        self.assertEqual((s.text, s.target.platform), ("Go to Lyon via Dijon", "telegram"))

    def test_cli_via(self):
        spec_text = "Wake up at 4pm via discord"
        record, _ = self.cmds.create_from_cli(spec_text)
        self.assertEqual(record.target, DISCORD_HOME)
        self.homes = {}
        record, message = self.cmds.create_from_cli(spec_text)
        self.assertIsNone(record)
        self.assertIn("/sethome", message)


class ToolViaTest(Base):
    def setUp(self):
        super().setUp()
        self.env = {
            "HERMES_SESSION_PLATFORM": "telegram",
            "HERMES_SESSION_CHAT_ID": "123",
            "HERMES_SESSION_USER_ID": "42",
        }

    def call(self, **args):
        return json.loads(self.cmds.tool({"text": "Wake up", "first_at": "2026-11-07T16:00", **args}))

    def test_platform_field_picks_the_home_channel(self):
        result = self.call(platform="Discord")
        self.assertTrue(result["ok"])
        self.assertIn("Discord (home channel)", result["message"])
        self.assertEqual(self.store.load()[0].target, DISCORD_HOME)

    def test_no_platform_keeps_the_session_chat(self):
        self.call()
        self.assertEqual(self.store.load()[0].target, Target("telegram", "123", "42"))

    def test_platform_without_home_channel_is_refused(self):
        self.homes = {}
        result = self.call(platform="discord")
        self.assertIn("/sethome", result["error"])
        self.assertEqual(self.store.load(), [])

    def test_unknown_platform_is_refused(self):
        for value in ("dijon", "whatsapp", 5):
            with self.subTest(value=value):
                self.assertIn("platform must be one of", self.call(platform=value)["error"])
        self.assertEqual(self.store.load(), [])

    def test_schema_lists_the_platform_field(self):
        self.assertIn("platform", commands.TOOL_SCHEMA["parameters"]["properties"])


class ConfirmRoundTripTest(Base):
    """A stored rule is read back by confirm.match; `every` is in minutes."""

    config = {"validate_setup": False}
    ME = confirm.Sender(platform="telegram", chat_id="123", user_id="42")

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
        self.assertIn("#2 Waiting one — waiting for yes (answer by today 11:10)", reply)
        self.assertNotIn("Done one", reply)
        self.assertNotIn("Cancelled one", reply)

    def test_expired_pending_is_flagged(self):
        self.add(state=PENDING, created=(NOW - timedelta(minutes=11)).isoformat())
        self.assertIn("expired", self.run_cmd("list"))

    def test_last_error_is_shown(self):
        self.add(last_error="chat not found", next_at=NOW.isoformat())
        self.assertIn("last error: chat not found", self.run_cmd("list"))

    def test_recurring_item_shows_days_and_next_occurrence(self):
        self.add(
            text="Pill A",
            schedule=Schedule(
                first_at="2026-11-07T07:00:00+00:00",
                every=10,
                tz="Europe/Paris",
                days=list(range(7)),
                span=120,
            ),
            next_at="2026-11-08T07:00:00+00:00",
        )
        self.assertIn("#1 Pill A — daily, next tomorrow 08:00, 0 sent", self.run_cmd("list"))

    def test_paused_is_the_first_line(self):
        self.paused = True
        self.add(next_at=(NOW + timedelta(minutes=5)).isoformat())
        self.assertEqual(self.run_cmd("list").splitlines()[0], commands.PAUSED_TEXT)

    def test_paused_with_nothing_running(self):
        self.paused = True
        self.assertEqual(self.run_cmd("list"), f"{commands.PAUSED_TEXT}\nNo snoozes running.")

    def test_not_paused_has_no_paused_line(self):
        self.add(next_at=(NOW + timedelta(minutes=5)).isoformat())
        self.assertNotIn("paused", self.run_cmd("list"))


class OkTest(Base):
    def test_ok_marks_done(self):
        self.add(next_at=NOW.isoformat())
        self.assertEqual(self.run_cmd("ok"), "✅ Done: Wake up")
        s = self.store.load()[0]
        self.assertEqual((s.state, s.next_at), (DONE, None))

    def test_ok_rearms_recurring_snooze_for_tomorrow_and_resets_count(self):
        self.add(
            schedule=Schedule(
                first_at="2026-11-07T07:00:00+00:00",
                every=10,
                until="2026-11-07T09:00:00+00:00",
                tz="Europe/Paris",
                days=list(range(7)),
                span=120,
            ),
            next_at=NOW.isoformat(),
            sent_count=3,
        )
        self.assertEqual(self.run_cmd("ok"), "✅ Done for today: Wake up. Next: tomorrow 08:00")
        s = self.store.load()[0]
        self.assertEqual((s.state, s.next_at, s.sent_count), (ACTIVE, "2026-11-08T07:00:00+00:00", 0))

    def test_cancel_ends_a_recurring_series(self):
        s = self.add(schedule=Schedule(first_at=NOW.isoformat(), days=list(range(7)), span=120))
        self.assertIn("Cancelled", self.run_cmd(f"cancel {s.id}"))
        self.assertEqual(self.store.get(s.id).state, CANCELLED)

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

    def test_ok_with_then_in_a_chat_says_now_and_runs_it_for_the_snooze_chat(self):
        self.add(then="close ticket 42", next_at=NOW.isoformat())
        calls = []
        cmds = Commands(self.ctx, data_dir=lambda: self.dir, now=lambda: NOW, run_then=lambda s, src: calls.append(s))
        reply = cmds.command("ok", source=telegram_source())
        self.assertEqual(reply, "✅ Done: Wake up. Now: close ticket 42…")
        self.assertEqual([(s.id, s.then, s.state) for s in calls], [(1, "close ticket 42", DONE)])

    def test_ok_with_then_from_the_cli_does_not_run_it_and_says_so(self):
        self.add(then="close ticket 42", next_at=NOW.isoformat())
        calls = []
        cmds = Commands(self.ctx, data_dir=lambda: self.dir, now=lambda: NOW, run_then=lambda s, src: calls.append(s))
        reply = cmds.command("ok", source=None)
        self.assertEqual(calls, [])
        self.assertEqual(reply, f"✅ Done: Wake up\n{commands.THEN_NOT_RUN_TEXT}")
        self.assertEqual(self.store.load()[0].state, DONE)

    def test_recurring_then_from_the_cli_keeps_the_next_occurrence_in_the_reply(self):
        self.add(
            then="close ticket 42",
            schedule=Schedule(
                first_at="2026-11-07T07:00:00+00:00",
                every=10,
                tz="Europe/Paris",
                days=list(range(7)),
                span=120,
            ),
            next_at=NOW.isoformat(),
        )
        reply = self.run_cmd("ok")
        self.assertIn("✅ Done for today: Wake up. Now: close ticket 42… Next: tomorrow 08:00", reply)
        self.assertTrue(reply.endswith(commands.THEN_NOT_RUN_TEXT))

    def test_ok_without_then_never_calls_the_runner(self):
        self.add(next_at=NOW.isoformat())
        calls = []
        cmds = Commands(self.ctx, data_dir=lambda: self.dir, now=lambda: NOW, run_then=lambda s, src: calls.append(s))
        self.assertEqual(cmds.command("ok", source=telegram_source()), "✅ Done: Wake up")
        self.assertEqual(calls, [])

    def test_ok_nothing_running(self):
        self.assertEqual(self.run_cmd("ok"), "No running snooze.")

    def test_ok_unknown_id(self):
        self.assertEqual(self.run_cmd("ok 9"), "No snooze #9.")

    def test_ok_on_pending_points_to_yes(self):
        self.add(state=PENDING)
        reply = self.run_cmd("ok 1")
        self.assertIn("has not started", reply)
        self.assertIn("`/snooze yes 1`", reply)
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
        # nothing sent yet: the second `later` pushes from the 11:00 it already has
        self.run_cmd("later 15m")
        self.assertEqual(self.store.load()[0].next_at, "2026-11-07T11:15:00+00:00")

    def test_later_too_long_is_refused_and_changes_nothing(self):
        self.add()
        before = self.store.load()[0].next_at
        for delay in ("87601h", "999999999999999h", "9" * 5000 + "m"):
            with self.subTest(delay=delay[:20]):
                self.assertIn("/snooze later", self.run_cmd(f"later {delay}"))
                self.assertEqual(self.store.load()[0].next_at, before)

    def test_later_without_number_uses_the_interval(self):
        self.add(schedule=Schedule(first_at=NOW.isoformat(), every=7, tz="Europe/Paris"))
        self.run_cmd("later")
        self.assertEqual(self.store.load()[0].next_at, "2026-11-07T10:07:00+00:00")

    def test_later_past_until_extends_until(self):
        until = NOW + timedelta(minutes=10)
        self.add(schedule=Schedule(first_at=NOW.isoformat(), every=7, until=until.isoformat(), tz="Europe/Paris"))
        self.run_cmd("later 30")
        saved = self.store.load()[0]
        self.assertEqual(saved.next_at, "2026-11-07T10:30:00+00:00")
        self.assertEqual(saved.schedule.until, "2026-11-07T11:40:00+00:00")  # 10:30 + 10 reminders * 7 min

    def test_later_reaching_until_exactly_promises_a_real_reminder(self):
        until = NOW + timedelta(minutes=10)
        self.add(
            schedule=Schedule(first_at=NOW.isoformat(), every=10, until=until.isoformat(), tz="Europe/Paris"),
            sent_count=1,
            next_at=until.isoformat(),
        )
        self.assertIn("back at 11:10", self.run_cmd("later 10"))
        saved = self.store.load()[0]
        self.assertEqual(saved.next_at, "2026-11-07T10:10:00+00:00")
        self.assertEqual(saved.schedule.until, "2026-11-07T11:40:00+00:00")  # 10:10 + 9 reminders left * 10 min
        at = NOW + timedelta(minutes=10)
        self.assertEqual(schedule.due(saved, at, 10).kind, schedule.SEND)

    def test_later_refused_when_the_reminder_cap_is_reached(self):
        self.add(sent_count=10, next_at=(NOW + timedelta(minutes=10)).isoformat())
        before = self.store.load()[0]
        reply = self.run_cmd("later 30")
        self.assertNotIn("back at", reply)
        self.assertIn("10 reminders", reply)
        self.assertEqual(self.store.load()[0], before)

    def test_later_uses_the_configured_reminder_cap(self):
        self.ctx.config["cap_reminders"] = 3
        self.add(sent_count=3, next_at=(NOW + timedelta(minutes=10)).isoformat())
        before = self.store.load()[0]
        self.assertIn("3 reminders", self.run_cmd("later 30"))
        self.assertEqual(self.store.load()[0], before)

    def test_later_allowed_below_the_cap(self):
        self.add(sent_count=9, next_at=(NOW + timedelta(minutes=10)).isoformat())
        self.assertIn("back at 11:30", self.run_cmd("later 30"))

    def test_later_on_a_recurring_snooze_keeps_the_next_occurrence_time(self):
        first = datetime(2026, 11, 7, 22, 30, tzinfo=timezone.utc)  # 23:30 in Paris
        self.now = first + timedelta(minutes=20)
        self.add(
            schedule=Schedule(
                first_at=first.isoformat(),
                every=10,
                until=(first + timedelta(hours=1)).isoformat(),
                tz="Europe/Paris",
                days=list(range(7)),
                span=60,
            ),
            sent_count=1,
            next_at=(first + timedelta(minutes=10)).isoformat(),
        )
        self.assertIn(
            "back at tomorrow 00:25", self.run_cmd("later 35")
        )  # crosses midnight: 23:25 UTC is 00:25 in Paris
        saved = self.store.load()[0]
        new_first = schedule.rearm(saved, self.now + timedelta(minutes=50))
        self.assertEqual(new_first, first + timedelta(days=1))

    def test_later_after_a_reminder_counts_from_now(self):
        # reminder sent at 10:00, next at 10:10; `later 5` at 10:01 -> 10:06, not 10:15
        self.now = NOW + timedelta(minutes=1)
        self.add(sent_count=1, next_at=(NOW + timedelta(minutes=10)).isoformat())
        self.assertIn("back at 11:06", self.run_cmd("later 5"))
        self.assertEqual(self.store.load()[0].next_at, "2026-11-07T10:06:00+00:00")

    def test_later_before_first_reminder_moves_it_later(self):
        # first reminder at 11:26 local (10:26 UTC); `later 10` said at 09:56 UTC -> 10:36 UTC, not 10:06
        self.now = NOW - timedelta(minutes=4)
        self.add(next_at=(NOW + timedelta(minutes=26)).isoformat())
        self.assertIn("back at 11:36", self.run_cmd("later 10"))
        self.assertEqual(self.store.load()[0].next_at, "2026-11-07T10:36:00+00:00")

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
        self.ctx.config["validate_setup"] = True
        self.run_cmd("Test in 1m every 1m", source=telegram_source())
        self.assertEqual(self.store.load()[0].state, PENDING)
        self.now = NOW + timedelta(minutes=2)
        self.assertIn("Started #1", self.run_cmd("yes"))
        s = self.store.load()[0]
        self.assertEqual((s.state, s.schedule.every), (ACTIVE, 1))
        self.assertEqual(s.next_at, "2026-11-07T10:01:00+00:00")


class SnoozeTestCommandTest(Base):
    def test_creates_a_running_one_shot_test_snooze(self):
        reply = self.run_cmd("test", source=telegram_source())
        s = self.store.load()[0]
        self.assertEqual((s.state, s.test, s.text), (ACTIVE, True, "🧪 Snooze test"))
        self.assertEqual((s.target.platform, s.target.chat_id), ("telegram", "123"))
        self.assertEqual(s.next_at, NOW.isoformat())
        self.assertEqual(s.schedule.until, (NOW + timedelta(minutes=2)).isoformat())
        self.assertEqual(s.confirm, {"kind": "ok", "value": ""})
        self.assertIn("✅ Gateway running for this profile", reply)
        self.assertIn("next 15 s", reply)
        self.assertIn("reply `Ok`", reply)

    def test_tick_constant_matches_the_text(self):
        from snooze import send

        self.assertEqual(echo.TEST_ARRIVES_IN, send.TICK_SECONDS)

    def test_sends_one_reminder_then_misses(self):
        self.run_cmd("test", source=telegram_source())
        s = self.store.load()[0]
        after_first = NOW + timedelta(seconds=15)
        schedule.after_send(s, after_first)
        self.assertEqual(schedule.due(s, after_first + timedelta(minutes=1), 10).kind, schedule.NOTHING)
        self.assertEqual(schedule.due(s, NOW + timedelta(minutes=2), 10).kind, schedule.MISS)
        self.assertEqual(echo.missed_text(s), echo.TEST_MISSED)

    def test_ok_passes_the_test(self):
        self.run_cmd("test", source=telegram_source())
        self.assertEqual(self.run_cmd("ok"), "✅ Test passed: reminders are sent and your replies are received.")
        self.assertEqual(self.store.load()[0].state, DONE)

    def test_no_test_without_a_gateway(self):
        self.gateway = False
        reply = self.run_cmd("test", source=telegram_source())
        self.assertIn("❌ Gateway not running", reply)
        self.assertEqual(self.store.load(), [])

    def test_gateway_unknown_still_runs_the_test(self):
        self.gateway = None
        reply = self.run_cmd("test", source=telegram_source())
        self.assertIn("could not check", reply)
        self.assertEqual(len(self.store.load()), 1)

    def test_goes_to_the_home_channel_from_the_cli(self):
        reply = self.run_cmd("test")
        self.assertEqual(self.store.load()[0].target, HOME)
        self.assertIn("home channel", reply)

    def test_no_target(self):
        self.home = None
        self.assertEqual(self.run_cmd("test"), commands.NO_TARGET_TEXT)
        self.assertEqual(self.store.load(), [])

    def test_words_after_test_are_a_reminder(self):
        self.run_cmd("test the alarm at 4pm", source=telegram_source())
        s = self.store.load()[0]
        self.assertEqual((s.text, s.test), ("test the alarm", False))


class GatewayWarningTest(Base):
    WARNING = echo.GATEWAY_OFF_WARNING

    def test_command_echo_warns_when_the_gateway_is_off(self):
        self.gateway = False
        self.assertIn(self.WARNING, self.run_cmd("Send email at 4pm", source=telegram_source()))

    def test_tool_echo_warns_when_the_gateway_is_off(self):
        self.gateway = False
        self.env = {"HERMES_SESSION_PLATFORM": "telegram", "HERMES_SESSION_CHAT_ID": "123"}
        result = json.loads(self.cmds.tool({"text": "Wake up", "first_at": "2026-11-07T16:00"}))
        self.assertIn(self.WARNING, result["message"])

    def test_no_warning_when_running_or_unknown(self):
        for state in (True, None):
            with self.subTest(state=state):
                self.gateway = state
                self.assertNotIn(self.WARNING, self.run_cmd("Send email at 4pm", source=telegram_source()))

    def test_running_in_the_gateway_process(self):
        with mock.patch("snooze.send.in_gateway", return_value=True):
            self.assertIs(commands.hermes_gateway_running(), True)

    def test_check_errors_give_none(self):
        with (
            mock.patch("snooze.send.in_gateway", return_value=False),
            mock.patch.dict("sys.modules", {"gateway.status": None}),  # makes the import fail
        ):
            self.assertIsNone(commands.hermes_gateway_running())


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


class ToolTest(Base):
    def call(self, **args):
        return json.loads(self.cmds.tool(args))

    def setUp(self):
        super().setUp()
        self.env = {
            "HERMES_SESSION_PLATFORM": "telegram",
            "HERMES_SESSION_CHAT_ID": "123",
            "HERMES_SESSION_USER_ID": "42",
        }

    def test_every_too_long_is_an_error(self):
        for every in ("87601h", "999999999999999h", 5256001, "9" * 5000):
            with self.subTest(every=str(every)[:20]):
                result = self.call(text="Wake up", first_at="2026-11-07T16:00", every=every)
                self.assertIn("every must be", result["error"])
        self.assertEqual(self.store.load(), [])

    def test_creates_a_pending_snooze_with_echo(self):
        self.ctx.config["validate_setup"] = True
        result = self.call(
            text="Send email to John Doe",
            first_at="2026-11-07T16:00",
            every="10m",
            until="2026-11-07T18:00",
            then="close ticket 42",
        )
        self.assertTrue(result["ok"])
        self.assertEqual((result["id"], result["state"]), (1, PENDING))
        self.assertIn("today 16:00", result["message"])
        self.assertIn("stops 18:00", result["message"])
        self.assertTrue(result["message"].endswith("Waiting:  not started. Reply `yes` within 10 min."))
        self.assertIn("NOT started", result["note"])
        self.assertIn("`/snooze yes 1`", result["note"])
        s = self.store.load()[0]
        self.assertEqual(s.schedule.first_at, "2026-11-07T15:00:00+00:00")
        self.assertEqual(s.schedule.until, "2026-11-07T17:00:00+00:00")
        self.assertEqual(s.schedule.every, 10)
        self.assertEqual(s.then, "close ticket 42")
        self.assertEqual(s.target, Target("telegram", "123", "42"))

    def test_starts_at_once_by_default(self):
        result = self.call(text="Wake up", first_at="2026-11-07T16:00")
        self.assertEqual(result["state"], ACTIVE)
        self.assertNotIn("`yes`", result["message"])
        s = self.store.load()[0]
        self.assertEqual(s.next_at, s.schedule.first_at)

    def test_pending_without_chat_uses_cli_wording(self):
        self.ctx.config["validate_setup"] = True
        self.env = {}  # no session chat: the home channel is the target
        result = self.call(text="Wake up", first_at="2026-11-07T16:00")
        self.assertTrue(
            result["message"].endswith(
                "Waiting:  not started. Type `/snooze yes 1` within 10 min (a bare `yes` only works in chat apps)."
            ),
            result["message"],
        )

    def test_defaults_match_the_command(self):
        self.call(text="Wake up", first_at="2026-11-07T16:00")
        s = self.store.load()[0]
        self.assertEqual(s.schedule.every, 10)
        self.assertEqual(s.schedule.until, "2026-11-07T17:00:00+00:00")
        self.assertEqual(s.confirm, {"kind": "ok", "value": ""})

    def test_recurrence_weekdays(self):
        self.call(text="Wake up", first_at="2026-11-09T08:00", recurrence="weekdays")
        self.assertEqual(self.store.load()[0].schedule.days, [0, 1, 2, 3, 4])

    def test_recurrence_everyday_is_daily(self):
        self.call(text="Wake up", first_at="2026-11-07T16:00", recurrence="everyday")
        self.assertEqual(self.store.load()[0].schedule.days, list(range(7)))

    def test_recurrence_moves_first_at_to_next_allowed_day_at_same_local_time(self):
        self.call(text="Wake up", first_at="2026-11-07T16:00", recurrence="mon,thu")
        s = self.store.load()[0]
        self.assertEqual(s.schedule.days, [0, 3])
        self.assertEqual(s.schedule.first_at, "2026-11-09T15:00:00+00:00")

    def test_invalid_recurrence_returns_error_without_creating(self):
        result = self.call(text="Wake up", first_at="2026-11-07T16:00", recurrence="someday")
        self.assertEqual(
            result["error"], "recurrence must be daily, weekdays, or days such as mon,thu or monday-friday."
        )
        self.assertEqual(self.store.load(), [])

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
        me = confirm.Sender(platform="telegram", chat_id="123", user_id="42")
        for value, said in (
            ("taken", "taken"),
            ("contains:sent", "I sent it"),
            ("any", "hmm"),
            ("👍", "👍"),
            ("ok", "ok"),
        ):
            with self.subTest(value=value):
                self.call(text="Wake up", first_at="2026-11-07T16:00", confirm=value)
                last = sent(self.store.load()[-1:])
                self.assertEqual(confirm.match(said, last, me).action, confirm.CONFIRM)

    def test_confirm_dict(self):
        self.call(text="Wake up", first_at="2026-11-07T16:00", confirm={"kind": "word", "value": "done"})
        self.assertEqual(self.store.load()[0].confirm, {"kind": "word", "value": "done"})

    def test_choice_confirm_string_and_dict(self):
        for value in ("a|b", {"kind": "choice", "value": "a|b"}):
            with self.subTest(value=value):
                result = self.call(text="Choose", first_at="2026-11-07T16:00", confirm=value)
                self.assertTrue(result["ok"])
                self.assertEqual(self.store.load()[-1].confirm, {"kind": "choice", "value": "a|b"})

    def test_choice_confirm_dict_rejects_non_choice_value(self):
        result = self.call(text="Choose", first_at="2026-11-07T16:00", confirm={"kind": "choice", "value": "a"})
        self.assertEqual(result["error"], commands._BAD_CONFIRM)
        self.assertEqual(self.store.load(), [])

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


class ToolRecurrenceUntilTest(Base):
    """`until` keeps its span after `first_at` when `first_at` moves to the next recurrence day."""

    def call(self, **args):
        return json.loads(self.cmds.tool(args))

    def setUp(self):
        super().setUp()
        self.env = {
            "HERMES_SESSION_PLATFORM": "telegram",
            "HERMES_SESSION_CHAT_ID": "123",
            "HERMES_SESSION_USER_ID": "42",
        }

    def test_until_follows_first_at_to_the_next_allowed_day(self):
        result = self.call(
            text="Wake up",
            first_at="2026-11-07T16:00",  # a Saturday
            until="2026-11-07T18:00",
            recurrence="weekdays",
        )
        self.assertTrue(result["ok"], result)
        s = self.store.load()[0]
        self.assertEqual(s.schedule.first_at, "2026-11-09T15:00:00+00:00")  # Monday 16:00 Paris
        self.assertEqual(s.schedule.until, "2026-11-09T17:00:00+00:00")
        self.assertEqual(s.schedule.span, 120)

    def test_until_before_first_at_is_still_refused_with_recurrence(self):
        result = self.call(
            text="Wake up",
            first_at="2026-11-07T16:00",
            until="2026-11-07T15:00",
            recurrence="weekdays",
        )
        self.assertEqual(result["error"], "until must be after first_at.")
        self.assertEqual(self.store.load(), [])


class MediaTagTest(Base):
    """A Hermes `MEDIA:` tag is neutralized in the text and in `then`, on both paths."""

    def test_command_text_and_then(self):
        self.create("Photo MEDIA:/tmp/a.png at 4pm then send MEDIA:/tmp/b.png")
        s = self.store.load()[0]
        self.assertEqual(s.text, "Photo MEDIA :/tmp/a.png")
        self.assertEqual(s.then, "send MEDIA :/tmp/b.png")

    def test_tool_text_and_then(self):
        self.env = {"HERMES_SESSION_PLATFORM": "telegram", "HERMES_SESSION_CHAT_ID": "123"}
        result = json.loads(
            self.cmds.tool(
                {
                    "text": "Photo MEDIA:/tmp/a.png",
                    "first_at": "2026-11-07T16:00",
                    "then": "send MEDIA:/tmp/b.png",
                }
            )
        )
        self.assertTrue(result["ok"])
        self.assertNotIn("MEDIA:", result["message"])
        s = self.store.load()[0]
        self.assertEqual((s.text, s.then), ("Photo MEDIA :/tmp/a.png", "send MEDIA :/tmp/b.png"))

    def test_text_without_tag_is_unchanged(self):
        self.create("Wake up at 4pm")
        self.assertEqual(self.store.load()[0].text, "Wake up")


class SeveralPlatformsTest(Base):
    """Telegram and Discord both have a home channel."""

    def setUp(self):
        super().setUp()
        self.platforms = ["telegram", "discord"]

    def add_one(self, **kw):
        self.add(next_at=(NOW + timedelta(minutes=5)).isoformat(), **kw)

    # the three outputs

    def help_lines(self, setting, source):
        self.ctx.config["notification_platform"] = setting
        return self.run_cmd("", source=source).splitlines()

    def test_help_says_where_a_telegram_chat_goes(self):
        lines = self.help_lines("source", telegram_source())
        self.assertEqual(lines[:-2], commands.HELP.splitlines()[:-1])
        self.assertEqual(
            lines[-2],
            "Reminders from here go to: this Telegram chat. Change: /snooze platform telegram|discord|source",
        )
        self.assertEqual(lines[-1], commands.HELP.splitlines()[-1])

    def test_help_says_a_cli_has_no_default_yet(self):
        lines = self.help_lines("source", None)
        self.assertEqual(
            lines[-2],
            "Reminders from here: no default yet. Set one: /snooze platform telegram|discord, "
            'or add "via telegram" or "via discord" to the text.',
        )
        self.assertEqual(lines[-1], commands.HELP.splitlines()[-1])

    def test_help_says_where_the_setting_sends_it(self):
        for source in (None, telegram_source()):
            with self.subTest(source=source):
                lines = self.help_lines("discord", source)
                self.assertEqual(
                    lines[-2],
                    "Reminders from here go to: Discord (home channel). "
                    "Change: /snooze platform telegram|discord|source",
                )

    def test_help_is_unchanged_with_one_platform(self):
        self.platforms = ["telegram"]
        self.assertEqual(self.run_cmd(""), commands.HELP)
        self.platforms = []
        self.assertEqual(self.run_cmd(""), commands.HELP)

    def test_echo_names_the_platform_for_a_chat_and_a_home(self):
        self.assertIn("Where:    Telegram (this chat)", self.run_cmd("Wake up at 4pm", source=telegram_source()))
        self.assertIn("Where:    Telegram (home channel)", self.run_cmd("Wake up at 4pm via telegram"))
        self.assertIn("Where:    Discord (home channel)", self.run_cmd("Wake up at 4pm via discord"))

    def test_list_names_each_platform(self):
        self.add_one(text="Mine")
        self.add_one(text="Theirs", target=DISCORD_HOME)
        self.add_one(text="Waiting", state=PENDING, target=DISCORD_HOME)
        reply = self.run_cmd("list")
        self.assertIn("#1 Mine — Telegram, next today 11:05, 0 sent", reply)
        self.assertIn("#2 Theirs — Discord, next today 11:05, 0 sent", reply)
        self.assertIn("#3 Waiting — Discord, waiting for yes", reply)

    def test_list_is_unchanged_with_one_platform(self):
        self.platforms = ["telegram"]
        self.add_one(text="Mine")
        self.assertIn("#1 Mine — next today 11:05, 0 sent", self.run_cmd("list"))

    # the CLI / no chat

    def test_cli_is_refused_with_both_ways_to_choose(self):
        record, message = self.cmds.create_from_cli("Wake up at 4pm")
        self.assertIsNone(record)
        self.assertIn("hermes snooze platform telegram", message)
        self.assertIn('hermes snooze add "Wake up at 4pm via telegram"', message)
        self.assertEqual(self.store.load(), [])

    def test_command_without_a_chat_is_refused_with_slash_commands(self):
        reply = self.run_cmd("Wake up at 4pm", source=None)
        self.assertEqual(
            reply,
            "⚠️ Where should reminders go? Telegram and Discord are both set up.\n"
            "Set a default once:  /snooze platform telegram\n"
            "Or for this one:     /snooze Wake up at 4pm via telegram\n"
            "Nothing was set.",
        )
        self.assertEqual(self.store.load(), [])

    def test_refusal_names_the_platforms_that_have_a_home(self):
        self.platforms = ["discord", "telegram", "slack"]
        reply = self.run_cmd("Wake up at 4pm", source=None)
        self.assertIn("Discord, Telegram and Slack are all set up.", reply)
        self.assertIn("/snooze platform discord\n", reply)

    def test_cli_refusal_names_terminal_commands(self):
        _, message = self.cmds.create_from_cli("Wake up at 4pm")
        self.assertEqual(
            message,
            "⚠️ Where should reminders go? Telegram and Discord are both set up.\n"
            "Set a default once:  hermes snooze platform telegram\n"
            'Or for this one:     hermes snooze add "Wake up at 4pm via telegram"\n'
            "Nothing was set.",
        )

    def test_test_command_without_a_chat_is_refused_without_a_text(self):
        reply = self.run_cmd("test", source=None)
        self.assertIn("Set a default once:  /snooze platform telegram\nNothing was set.", reply)
        self.assertNotIn("Or for this one", reply)
        self.assertEqual(self.store.load(), [])

    def test_tool_without_a_chat_is_refused_for_the_agent(self):
        result = json.loads(self.cmds.tool({"text": "Wake up", "first_at": "2026-11-07T16:00"}))
        self.assertEqual(
            result["error"],
            "Several platforms have a home channel (telegram, discord) and there is no chat. "
            "Ask the user which platform to use, then call snooze_create again with `platform`. Nothing was set.",
        )
        self.assertEqual(self.store.load(), [])

    def test_cli_escape_via(self):
        record, _ = self.cmds.create_from_cli("Wake up at 4pm via discord")
        self.assertEqual(record.target, DISCORD_HOME)

    def test_cli_escape_after_platform_command(self):
        self.assertEqual(self.cmds.platform("telegram")[0], True)
        record, _ = self.cmds.create_from_cli("Wake up at 4pm")
        self.assertEqual(record.target, HOME)

    def test_with_one_platform_the_cli_uses_the_only_home(self):
        self.platforms = ["telegram"]
        record, _ = self.cmds.create_from_cli("Wake up at 4pm")
        self.assertEqual(record.target, HOME)

    # chats and priority are unchanged

    def test_chat_command_answers_in_its_own_chat(self):
        source = SimpleNamespace(platform=SimpleNamespace(value="discord"), chat_id=555, user_id=7)
        self.assertEqual(self.create(source=source).target, Target("discord", "555", "7"))
        self.assertEqual(self.create(source=telegram_source()).target, Target("telegram", "123", "42"))

    def test_tool_in_a_chat_answers_in_that_chat(self):
        self.env = {"HERMES_SESSION_PLATFORM": "discord", "HERMES_SESSION_CHAT_ID": "555"}
        self.cmds.tool({"text": "Wake up", "first_at": "2026-11-07T16:00"})
        self.assertEqual(self.store.load()[0].target, Target("discord", "555", None))

    def test_setting_beats_the_chat_and_via_beats_the_setting(self):
        self.ctx.config["notification_platform"] = "discord"
        self.run_cmd("Wake up at 4pm", source=telegram_source())
        self.run_cmd("Wake up at 5pm via telegram", source=telegram_source())
        self.assertEqual([s.target for s in self.store.load()], [DISCORD_HOME, HOME])

    def test_setting_applies_to_the_cli(self):
        self.ctx.config["notification_platform"] = "discord"
        record, _ = self.cmds.create_from_cli("Wake up at 4pm")
        self.assertEqual(record.target, DISCORD_HOME)


class PlatformCommandTest(Base):
    def test_show_a_home_platform(self):
        self.ctx.config["notification_platform"] = "discord"
        self.assertEqual(
            self.run_cmd("platform"),
            "Default platform: Discord (home channel). Change: /snooze platform telegram | discord | source",
        )

    def test_show_source(self):
        self.assertTrue(self.run_cmd("platform").startswith("Default platform: the chat where you type (source)."))

    def test_set_each_value_through_the_ctx(self):
        for value in ("telegram", "discord", "source"):
            with self.subTest(value=value):
                reply = self.run_cmd(f"platform {value.upper()}")
                self.assertEqual(self.ctx.config["notification_platform"], value)
                self.assertTrue(reply.startswith("✅ Reminders go to "))
                self.assertEqual(commands.read_settings(self.ctx).notification_platform, value)

    def test_set_replies(self):
        self.assertEqual(
            self.run_cmd("platform telegram"),
            "✅ Reminders go to Telegram (home channel). Change: /snooze platform discord|source",
        )
        self.assertEqual(
            self.run_cmd("platform discord"),
            "✅ Reminders go to Discord (home channel). Change: /snooze platform telegram|source",
        )
        self.assertEqual(
            self.run_cmd("platform source"),
            "✅ Reminders go to the chat where you type (source). Change: /snooze platform telegram|discord",
        )

    def test_set_reply_in_the_terminal_names_the_hermes_command(self):
        self.assertEqual(
            self.cmds.platform("telegram", terminal=True)[1],
            "✅ Reminders go to Telegram (home channel). Change: hermes snooze platform discord|source",
        )

    def test_platform_without_a_home_channel_is_refused_and_nothing_is_written(self):
        self.homes = {"telegram": HOME}
        reply = self.run_cmd("platform discord")
        self.assertIn("No Discord home channel", reply)
        self.assertIn("/sethome", reply)
        self.assertNotIn("notification_platform", self.ctx.config)

    def test_source_needs_no_home_channel(self):
        self.homes = {}
        self.run_cmd("platform source")
        self.assertEqual(self.ctx.config["notification_platform"], "source")

    def test_bad_value_gets_the_usage_line(self):
        self.assertEqual(self.run_cmd("platform matrix"), commands.USAGE["platform"])
        self.assertEqual(self.run_cmd("platform discord telegram"), commands.USAGE["platform"])
        self.assertNotIn("notification_platform", self.ctx.config)

    def test_a_save_error_is_reported(self):
        self.ctx.set_config = mock.Mock(side_effect=PermissionError("managed install"))
        self.assertIn("managed install", self.run_cmd("platform discord"))

    def test_text_that_does_not_fit_is_still_a_reminder(self):
        self.run_cmd("platform meeting at 4pm", source=telegram_source())
        self.assertEqual(self.store.load()[0].text, "platform meeting")

    def test_listed_in_help_and_usage(self):
        self.assertIn("platform", commands.SUBCOMMANDS)
        self.assertIn("platform", commands.HELP)
        self.assertIn("platform", commands.USAGE)


class SettingsTest(unittest.TestCase):
    def test_values_are_read(self):
        ctx = FakeCtx(validate_setup=False, cap_minutes=30, cap_reminders=3, timezone="Asia/Tokyo")
        settings = commands.read_settings(ctx)
        self.assertEqual(
            (settings.validate_setup, settings.cap_minutes, settings.cap_reminders, settings.timezone),
            (False, 30, 3, "Asia/Tokyo"),
        )

    def test_notification_platform_is_read_and_validated(self):
        discord = commands.read_settings(FakeCtx(notification_platform="Discord"))
        invalid = commands.read_settings(FakeCtx(notification_platform="matrix"))
        self.assertEqual(discord.notification_platform, "discord")
        self.assertEqual(invalid.notification_platform, "source")

    def test_invalid_values_fall_back(self):
        ctx = FakeCtx(validate_setup="maybe", cap_minutes=-5, cap_reminders="lots", timezone="Mars/Base")
        settings = commands.read_settings(ctx)
        self.assertEqual((settings.validate_setup, settings.cap_minutes, settings.cap_reminders), (False, 120, 10))
        self.assertEqual(settings.timezone, commands.system_timezone())

    def test_caps_apply_to_new_snoozes(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        ctx = FakeCtx(cap_minutes=30, cap_reminders=3)
        cmds = Commands(ctx, data_dir=lambda: tmp.name, now=lambda: NOW, home_target=lambda: HOME)
        echo = cmds.command("Wake up at 4pm every 10m", source=telegram_source())
        self.assertIn("stops 16:30 or after 3 reminders", echo)

    def test_discord_setting_routes_chat_command_to_discord_home(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        discord_home = Target("discord", "channel-1", "user-1")
        ctx = FakeCtx(notification_platform="discord")
        home_target = mock.Mock(return_value=discord_home)
        cmds = Commands(ctx, data_dir=lambda: tmp.name, now=lambda: NOW, home_target=home_target)

        cmds.command("Wake up in 10m", source=telegram_source())

        self.assertEqual(Store(tmp.name).load()[0].target, discord_home)
        home_target.assert_called_once_with("discord")

    def test_discord_setting_routes_tool_created_snooze_to_discord_home(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        discord_home = Target("discord", "channel-1", "user-1")
        home_target = mock.Mock(return_value=discord_home)
        cmds = Commands(
            FakeCtx(notification_platform="discord"),
            data_dir=lambda: tmp.name,
            now=lambda: NOW,
            home_target=home_target,
        )

        result = json.loads(cmds.tool({"text": "Wake up", "first_at": "2026-11-07T10:10:00Z"}))

        self.assertTrue(result["ok"])
        self.assertEqual(Store(tmp.name).load()[0].target, discord_home)
        home_target.assert_called_once_with("discord")

    def test_selected_platform_without_home_channel_does_not_create_snooze(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        cmds = Commands(
            FakeCtx(notification_platform="discord"),
            data_dir=lambda: tmp.name,
            now=lambda: NOW,
            home_target=mock.Mock(return_value=None),
        )

        self.assertIn("No Discord home channel is configured", cmds.command("Wake up in 10m", source=telegram_source()))
        self.assertEqual(Store(tmp.name).load(), [])


if __name__ == "__main__":
    unittest.main()
