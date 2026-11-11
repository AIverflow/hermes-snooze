import unittest
from datetime import datetime, timedelta, timezone

from snooze import confirm, echo
from snooze.store import ACTIVE, PENDING, Schedule, Snooze, Target

NOW = datetime(2026, 11, 7, 10, 0, tzinfo=timezone.utc)  # 11:00 in Paris


class FormatTest(unittest.TestCase):
    def test_format_minutes(self):
        self.assertEqual(echo.format_minutes(10), "10 min")
        self.assertEqual(echo.format_minutes(60), "1 h")
        self.assertEqual(echo.format_minutes(90), "1 h 30 min")
        self.assertEqual(echo.format_minutes(1.5), "1 min 30 s")

    def test_format_when_labels(self):
        zone = "Europe/Paris"
        self.assertEqual(echo.format_when(NOW, zone, NOW), "today 11:00")
        self.assertEqual(echo.format_when(NOW + timedelta(days=1), zone, NOW), "tomorrow 11:00")
        self.assertEqual(echo.format_when(NOW + timedelta(days=3), zone, NOW), "Tue 10 Nov 11:00")
        self.assertEqual(echo.format_when(NOW + timedelta(hours=1), zone, NOW, relative_to=NOW), "12:00")

    def test_local_day_not_utc_day(self):
        late = datetime(2026, 11, 7, 23, 30, tzinfo=timezone.utc)  # 00:30 on the 8th in Paris
        self.assertEqual(echo.format_when(late, "Europe/Paris", NOW), "tomorrow 00:30")

    def test_days_text(self):
        self.assertEqual(echo.days_text(range(7)), "daily")
        self.assertEqual(echo.days_text(range(5)), "weekdays")
        self.assertEqual(echo.days_text([0, 3]), "Mon, Thu")


class InvalidAnswerTextTest(unittest.TestCase):
    def snooze(self, confirm=None, platform="telegram", text="drink water"):
        return Snooze(
            id=31,
            text=text,
            created=NOW.isoformat(),
            target=Target(platform, "1", "1"),
            schedule=Schedule(first_at=NOW.isoformat(), every=10),
            confirm=confirm,
        )

    def test_ok_snooze(self):
        self.assertEqual(
            echo.invalid_answer_text(self.snooze(), "okkkk"),
            '"okkkk" is not a valid answer for #31 (drink water).\n'
            "Reply: Ok · later · later 15     Or type: /snooze ok 31 · /snooze cancel 31",
        )

    def test_word_and_emoji_snooze(self):
        word = echo.invalid_answer_text(self.snooze({"kind": "word", "value": "done"}), "x")
        self.assertIn("Reply: done · later · later 15     Or type: /snooze ok 31 · /snooze cancel 31", word)
        emoji = echo.invalid_answer_text(self.snooze({"kind": "emoji", "value": "👍"}), "x")
        self.assertIn("Reply: 👍 · later · later 15", emoji)

    def test_choice_snooze_has_no_ok_command(self):
        text = echo.invalid_answer_text(self.snooze({"kind": "choice", "value": "merge|review"}), "x")
        self.assertIn("Reply: merge · review · 1 · 2 · later · later 15     Or type: /snooze cancel 31", text)
        self.assertNotIn("/snooze ok", text)

    def test_discord_wording(self):
        text = echo.invalid_answer_text(self.snooze(platform="discord"), "x")
        self.assertIn("Reply (to a reminder): Ok · later · later 15", text)

    def test_the_typed_text_is_cut_and_kept_on_one_line(self):
        text = echo.invalid_answer_text(self.snooze(), "a\n" + "b" * 60)
        self.assertEqual(text.splitlines()[0].split(" is not")[0], '"a ' + "b" * 38 + '…"')

    def test_old_any_rule_lists_ok(self):
        self.assertIn("Reply: Ok · later", echo.invalid_answer_text(self.snooze({"kind": "any", "value": ""}), "x"))


class ReminderTextTest(unittest.TestCase):
    def snooze(self, sent_count=0):
        return Snooze(
            id=14,
            text="Stretch",
            created=NOW.isoformat(),
            target=Target("telegram", "1", "1"),
            schedule=Schedule(first_at=NOW.isoformat(), every=10),
            sent_count=sent_count,
        )

    def test_first_reminder_says_how_to_answer(self):
        self.assertEqual(echo.reminder_text(self.snooze()), '🔔 Snooze #14: Stretch  (reply "Ok" | later)')

    def test_first_reminder_instruction_matches_the_confirm_rule(self):
        cases = [
            ({"kind": "word", "value": "taken"}, 'reply "taken"', "taken"),
            ({"kind": "emoji", "value": "✅"}, "send ✅", "✅"),
        ]
        for rule, instruction, answer in cases:
            with self.subTest(rule=rule):
                snooze = self.snooze()
                snooze.confirm = rule
                snooze.state = ACTIVE
                snooze.last_sent_at = NOW.isoformat()
                text = echo.reminder_text(snooze)
                self.assertIn(f"({instruction} | later)", text)
                self.assertNotIn('reply "Ok"', text)
                decision = confirm.match(answer, [snooze], confirm.Sender("telegram", "1", "1"))
                self.assertEqual(decision.action, confirm.CONFIRM)

    def test_choice_instruction_covers_every_option(self):
        for count in (2, 4, 9):
            with self.subTest(count=count):
                snooze = self.snooze()
                snooze.confirm = {"kind": "choice", "value": "|".join(f"option{i}" for i in range(1, count + 1))}
                text = echo.reminder_text(snooze)
                self.assertIn(f"reply 1-{count} | later", text)
                self.assertIn(f"{count} option{count}", text)

    def test_next_reminders_are_numbered(self):
        for sent, label in ((1, "2nd"), (2, "3rd"), (3, "4th"), (10, "11th"), (11, "12th"), (12, "13th"), (20, "21st")):
            with self.subTest(sent=sent):
                self.assertEqual(echo.reminder_text(self.snooze(sent)), f"🔔 Snooze #14: Stretch ({label})")

    def test_late_reminder_adds_a_line(self):
        text = echo.reminder_text(self.snooze(1), timedelta(minutes=40))
        self.assertEqual(text, "🔔 Snooze #14: Stretch (2nd)\n⏰ late by 40 min")

    def test_late_by_hours(self):
        self.assertIn("⏰ late by 3 h", echo.reminder_text(self.snooze(), timedelta(hours=3, seconds=20)))

    def test_choice_reminders_show_numbered_options(self):
        snooze = self.snooze()
        snooze.text = "Dinner?"
        snooze.confirm = {"kind": "choice", "value": "asian|french|italian"}
        self.assertEqual(
            echo.reminder_text(snooze),
            "🔔 Snooze #14: Dinner?  (reply 1-3 | later)\n1 asian | 2 french | 3 italian",
        )
        snooze.sent_count = 1
        self.assertEqual(
            echo.reminder_text(snooze),
            "🔔 Snooze #14: Dinner? (2nd)\n1 asian | 2 french | 3 italian",
        )

    def test_old_any_and_contains_rules_read_as_ok(self):
        for rule in ({"kind": "any", "value": ""}, {"kind": "contains", "value": "sent"}):
            with self.subTest(rule=rule):
                snooze = self.snooze()
                snooze.confirm = rule
                self.assertEqual(echo.reminder_text(snooze), '🔔 Snooze #14: Stretch  (reply "Ok" | later)')

    def test_missed_text(self):
        self.assertEqual(echo.missed_text(self.snooze(3)), "⌛ Missed: Stretch (3 reminders, no answer)")
        self.assertEqual(echo.missed_text(self.snooze(1)), "⌛ Missed: Stretch (1 reminder, no answer)")

    def test_missed_text_of_a_test_snooze(self):
        sn = self.snooze(1)
        sn.test = True
        self.assertEqual(echo.missed_text(sn), echo.TEST_MISSED)
        self.assertIn("no reply received in 2 min", echo.missed_text(sn))


class GatewayWarningTest(unittest.TestCase):
    def render(self, **kw):
        sn = Snooze(
            id=1,
            text="Stretch",
            created=NOW.isoformat(),
            target=Target("telegram", "1", "1"),
            schedule=Schedule(first_at=NOW.isoformat(), every=10, until=(NOW + timedelta(hours=1)).isoformat()),
            confirm={"kind": "ok", "value": ""},
        )
        return echo.render_echo(sn, NOW, "this chat", 10, timedelta(minutes=10), **kw)

    def test_warning_is_the_last_line_when_the_gateway_is_off(self):
        self.assertEqual(self.render(gateway_off=True).splitlines()[-1], echo.GATEWAY_OFF_WARNING)

    def test_no_warning_by_default(self):
        self.assertNotIn("gateway", self.render())

    def test_explicit_until_still_shows_the_reminder_cap(self):
        self.assertIn("or after 10 reminders", self.render())

    def test_choice_stop_text_lists_numbered_options(self):
        sn = Snooze(
            id=1,
            text="Dinner?",
            created=NOW.isoformat(),
            target=Target("telegram", "1", "1"),
            schedule=Schedule(first_at=NOW.isoformat()),
            confirm={"kind": "choice", "value": "asian|french|italian"},
        )
        text = echo.render_echo(sn, NOW, "this chat", 10, timedelta(minutes=10))
        self.assertIn('Stop it:  reply with one of: 1 asian | 2 french | 3 italian | "later 30" to push it back', text)
        self.assertIn("Answer in chat: reply with an option", text)
        self.assertNotIn("/snooze ok 1", text)


class StartStateTest(unittest.TestCase):
    def render(self, state, where="this chat"):
        sn = Snooze(
            id=3,
            text="Stretch",
            created=NOW.isoformat(),
            target=Target("telegram", "1", "1"),
            schedule=Schedule(first_at=(NOW + timedelta(hours=1)).isoformat(), every=10, tz="Europe/Paris"),
            confirm={"kind": "ok", "value": ""},
            state=state,
        )
        return echo.render_echo(sn, NOW, where, 10, timedelta(minutes=10))

    def test_started_has_no_yes_line_and_undo_is_last(self):
        lines = self.render(ACTIVE).splitlines()
        self.assertFalse(any("yes" in line for line in lines))
        self.assertEqual(lines[-1], "Undo:     /snooze cancel 3")

    def test_pending_in_chat_has_one_reply_yes_line(self):
        lines = self.render(PENDING).splitlines()
        self.assertEqual(lines[-2], "Undo:     /snooze cancel 3")
        self.assertEqual(lines[-1], "Waiting:  not started. Reply `yes` within 10 min.")

    def test_pending_in_cli_says_type_the_command(self):
        lines = self.render(PENDING, where="home channel").splitlines()
        self.assertEqual(
            lines[-1],
            "Waiting:  not started. Type `/snooze yes 3` within 10 min (a bare `yes` only works in chat apps).",
        )
        self.assertEqual(sum(line.startswith("Waiting:") for line in lines), 1)


if __name__ == "__main__":
    unittest.main()
