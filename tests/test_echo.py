import unittest
from datetime import datetime, timedelta, timezone

from snooze import echo

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


if __name__ == "__main__":
    unittest.main()
