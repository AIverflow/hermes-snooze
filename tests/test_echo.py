import unittest
from datetime import datetime, timedelta, timezone

from snooze import echo
from snooze.store import Schedule, Snooze, Target

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


class ReminderTextTest(unittest.TestCase):
    def snooze(self, sent_count=0):
        return Snooze(
            text="Stretch",
            created=NOW.isoformat(),
            target=Target("telegram", "1", "1"),
            schedule=Schedule(first_at=NOW.isoformat(), every=10),
            sent_count=sent_count,
        )

    def test_first_reminder_says_how_to_answer(self):
        self.assertEqual(echo.reminder_text(self.snooze()), "🔔 Stretch  (reply ok / later)")

    def test_next_reminders_are_numbered(self):
        for sent, label in ((1, "2nd"), (2, "3rd"), (3, "4th"), (10, "11th"), (11, "12th"), (12, "13th"), (20, "21st")):
            with self.subTest(sent=sent):
                self.assertEqual(echo.reminder_text(self.snooze(sent)), f"🔔 Stretch ({label})")

    def test_late_reminder_adds_a_line(self):
        text = echo.reminder_text(self.snooze(1), timedelta(minutes=40))
        self.assertEqual(text, "🔔 Stretch (2nd)\n⏰ late by 40 min")

    def test_late_by_hours(self):
        self.assertIn("⏰ late by 3 h", echo.reminder_text(self.snooze(), timedelta(hours=3, seconds=20)))

    def test_missed_text(self):
        self.assertEqual(echo.missed_text(self.snooze(3)), "⌛ Missed: Stretch (3 reminders, no answer)")
        self.assertEqual(echo.missed_text(self.snooze(1)), "⌛ Missed: Stretch (1 reminder, no answer)")


if __name__ == "__main__":
    unittest.main()
