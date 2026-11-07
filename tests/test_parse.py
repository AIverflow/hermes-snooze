import unittest
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from snooze import parse as p

PARIS = ZoneInfo("Europe/Paris")
NY = ZoneInfo("America/New_York")
UTC = timezone.utc

# Saturday 2026-11-07 10:00 in Paris (CET, UTC+1).
NOW = datetime(2026, 11, 7, 10, 0, tzinfo=PARIS)


def parse(text, now=NOW, tz=PARIS):
    return p.parse(text, now, tz)


def paris(h, m=0, day=7):
    return datetime(2026, 11, day, h, m, tzinfo=PARIS).astimezone(UTC)


class PlanTableTest(unittest.TestCase):
    """One test per row of the examples table."""

    def test_short_form(self):
        s = parse("Wake up 8am 10m")
        self.assertEqual(s.text, "Wake up")
        self.assertEqual(s.first_at, paris(8, day=8))  # 10:00 now, so 8am is tomorrow
        self.assertEqual(s.every, timedelta(minutes=10))
        self.assertIsNone(s.until)
        self.assertEqual(s.confirm, p.ConfirmRule("ok"))
        self.assertIsNone(s.then)
        self.assertEqual(s.tz, "Europe/Paris")

    def test_long_form_same_as_short(self):
        self.assertEqual(parse("Wake up at 8am every 10m"), parse("Wake up 8am 10m"))

    def test_in_every_ok_word(self):
        s = parse("Take my pill in 30m every 5m ok taken")
        self.assertEqual(s.text, "Take my pill")
        self.assertEqual(s.first_at, NOW.astimezone(UTC) + timedelta(minutes=30))
        self.assertEqual(s.every, timedelta(minutes=5))
        self.assertEqual(s.confirm, p.ConfirmRule("word", "taken"))

    def test_full_example(self):
        s = parse("Send email to John Doe at 4pm every 10m until 6pm then close ticket 42")
        self.assertEqual(s.text, "Send email to John Doe")
        self.assertEqual(s.first_at, paris(16))
        self.assertEqual(s.every, timedelta(minutes=10))
        self.assertEqual(s.until, paris(18))
        self.assertEqual(s.then, "close ticket 42")
        self.assertEqual(s.confirm, p.ConfirmRule("ok"))

    def test_short_form_with_then(self):
        s = parse("Wake up 8am 10m then start the coffee machine")
        self.assertEqual(s.text, "Wake up")
        self.assertEqual(s.every, timedelta(minutes=10))
        self.assertEqual(s.then, "start the coffee machine")


class ConfirmTableTest(unittest.TestCase):
    def confirm(self, tail):
        s = parse("Do it at 4pm " + tail)
        self.assertIsInstance(s, p.Spec, s)
        self.assertEqual(s.text, "Do it")
        return s.confirm

    def test_default(self):
        self.assertEqual(parse("Do it at 4pm").confirm, p.ConfirmRule("ok", ""))

    def test_plain_ok_is_default(self):
        self.assertEqual(self.confirm("ok"), p.ConfirmRule("ok"))

    def test_word(self):
        self.assertEqual(self.confirm("ok taken"), p.ConfirmRule("word", "taken"))

    def test_contains(self):
        self.assertEqual(self.confirm("ok contains:sent"), p.ConfirmRule("contains", "sent"))

    def test_any(self):
        self.assertEqual(self.confirm("ok any"), p.ConfirmRule("any"))

    def test_emoji(self):
        self.assertEqual(self.confirm("ok \U0001F44D"), p.ConfirmRule("emoji", "\U0001F44D"))

    def test_emoji_with_variation_selector(self):
        self.assertEqual(self.confirm("ok ✔️"), p.ConfirmRule("emoji", "✔️"))


class KeywordOrderTest(unittest.TestCase):
    def test_any_order(self):
        a = parse("Call Bob until 6pm ok done every 10m at 4pm then hang up")
        b = parse("Call Bob at 4pm every 10m until 6pm ok done then hang up")
        self.assertIsInstance(a, p.Spec)
        self.assertEqual(a, b)

    def test_keywords_case_insensitive(self):
        s = parse("Call Bob AT 4PM EVERY 10M")
        self.assertEqual(s.first_at, paris(16))
        self.assertEqual(s.every, timedelta(minutes=10))

    def test_ok_in_middle_has_no_argument(self):
        s = parse("Call Bob at 4pm ok every 10m")
        self.assertEqual(s.confirm, p.ConfirmRule("ok"))
        self.assertEqual(s.every, timedelta(minutes=10))
        self.assertEqual(s.text, "Call Bob")

    def test_then_takes_everything_after_it(self):
        s = parse("Call Bob at 4pm then email Ann at 5pm every 1m ok yes")
        self.assertEqual(s.then, "email Ann at 5pm every 1m ok yes")
        self.assertEqual(s.first_at, paris(16))
        self.assertIsNone(s.every)
        self.assertEqual(s.confirm, p.ConfirmRule("ok"))

    def test_first_word_then_is_text(self):
        s = parse("then what at 4pm")
        self.assertEqual(s.text, "then what")

    def test_inner_spacing_kept_in_text(self):
        s = parse("  Send   email\tto Bob   at 4pm  ")
        self.assertEqual(s.text, "Send   email\tto Bob")


class UnknownWordsStayInTextTest(unittest.TestCase):
    def test_before_6pm(self):
        s = parse("Send email before 6pm at 4pm")
        self.assertEqual(s.text, "Send email before 6pm")
        self.assertEqual(s.first_at, paris(16))
        self.assertIsNone(s.until)

    def test_before_6pm_with_every(self):
        s = parse("Send email before 6pm at 4pm every 10m")
        self.assertEqual(s.text, "Send email before 6pm")
        self.assertEqual(s.every, timedelta(minutes=10))

    def test_before_6pm_not_a_short_form_time(self):
        self.assertIsInstance(parse("Send email before 6pm"), p.ParseError)

    def test_in_a_place_stays_in_text(self):
        s = parse("Meet me in Paris at 4pm")
        self.assertEqual(s.text, "Meet me in Paris")

    def test_repeated_keyword_is_text(self):
        s = parse("Dinner at 7pm at 6pm")
        self.assertEqual(s.text, "Dinner at 7pm")
        self.assertEqual(s.first_at, paris(18))

    def test_at_and_in_together_second_one_stays_in_text(self):
        s = parse("Meet at 5pm in 10m")
        self.assertEqual(s.text, "Meet at 5pm")
        self.assertEqual(s.first_at, NOW.astimezone(UTC) + timedelta(minutes=10))


class TimeTest(unittest.TestCase):
    def first(self, when, now=NOW):
        s = parse("X " + when, now=now)
        self.assertIsInstance(s, p.Spec, s)
        return s.first_at

    def test_formats(self):
        self.assertEqual(self.first("at 4pm"), paris(16))
        self.assertEqual(self.first("at 16:00"), paris(16))
        self.assertEqual(self.first("at 4:30pm"), paris(16, 30))
        self.assertEqual(self.first("at 4PM"), paris(16))
        self.assertEqual(self.first("at 11:59"), paris(11, 59))

    def test_noon_and_midnight(self):
        self.assertEqual(self.first("at 12pm"), paris(12))
        self.assertEqual(self.first("at 12am"), paris(0, day=8))

    def test_past_time_rolls_to_tomorrow(self):
        self.assertEqual(self.first("at 8am"), paris(8, day=8))
        self.assertEqual(self.first("at 9:59"), paris(9, 59, day=8))

    def test_exactly_now_rolls_to_tomorrow(self):
        self.assertEqual(self.first("at 10:00"), paris(10, day=8))

    def test_future_time_is_today(self):
        self.assertEqual(self.first("at 10:01"), paris(10, 1))

    def test_now_in_other_zone(self):
        now_utc = datetime(2026, 11, 7, 9, 0, tzinfo=UTC)  # 10:00 in Paris
        self.assertEqual(self.first("at 11am", now=now_utc), paris(11))

    def test_result_is_utc(self):
        self.assertEqual(parse("X at 4pm").first_at.utcoffset(), timedelta(0))

    def test_tz_given_as_name(self):
        s = p.parse("X at 4pm", NOW, "Europe/Paris")
        self.assertEqual(s.first_at, paris(16))
        self.assertEqual(s.tz, "Europe/Paris")

    def test_until_after_midnight_is_next_day(self):
        s = parse("X at 11pm until 1am")
        self.assertEqual(s.until, paris(1, day=8))

    def test_until_same_clock_time_is_next_day(self):
        s = parse("X at 4pm until 4pm")
        self.assertEqual(s.until, paris(16, day=8))

    def test_until_with_in(self):
        s = parse("X in 30m until 11am")
        self.assertEqual(s.until, paris(11))


class DurationTest(unittest.TestCase):
    def every(self, d):
        s = parse(f"X at 4pm every {d}")
        self.assertIsInstance(s, p.Spec, s)
        return s.every

    def test_units(self):
        self.assertEqual(self.every("10m"), timedelta(minutes=10))
        self.assertEqual(self.every("1h"), timedelta(hours=1))
        self.assertEqual(self.every("90s"), timedelta(seconds=90))
        self.assertEqual(self.every("1h30m"), timedelta(minutes=90))
        self.assertEqual(self.every("5min"), timedelta(minutes=5))
        self.assertEqual(self.every("60s"), timedelta(minutes=1))

    def test_below_one_minute_is_error(self):
        for bad in ("59s", "30s", "0m"):
            with self.subTest(bad=bad):
                e = parse(f"X at 4pm every {bad}")
                self.assertIsInstance(e, p.ParseError)
                self.assertIn("1 minute", e.message)

    def test_in_below_one_minute_is_error(self):
        self.assertIsInstance(parse("X in 30s"), p.ParseError)

    def test_short_form_below_one_minute_is_error(self):
        self.assertIsInstance(parse("Wake up 8am 30s"), p.ParseError)

    def test_in_hours(self):
        s = parse("X in 2h")
        self.assertEqual(s.first_at, NOW.astimezone(UTC) + timedelta(hours=2))
        self.assertIsNone(s.every)


class DstTest(unittest.TestCase):
    """America/New_York: clocks jump 02:00 -> 03:00 on 2026-03-08 and 02:00 -> 01:00 on 2026-11-01."""

    def ny(self, text, now):
        s = p.parse(text, now, NY)
        self.assertIsInstance(s, p.Spec, s)
        return s

    def test_spring_forward_rollover_uses_new_offset(self):
        now = datetime(2026, 3, 7, 20, 0, tzinfo=NY)  # EST
        s = self.ny("X at 8am", now)
        # 8am EDT (UTC-4) = 12:00 UTC. Adding 24h would wrongly give 9am EDT.
        self.assertEqual(s.first_at, datetime(2026, 3, 8, 12, 0, tzinfo=UTC))

    def test_spring_forward_same_day(self):
        now = datetime(2026, 3, 8, 0, 30, tzinfo=NY)  # EST
        s = self.ny("X at 8am until 9am", now)
        self.assertEqual(s.first_at, datetime(2026, 3, 8, 12, 0, tzinfo=UTC))
        self.assertEqual(s.until, datetime(2026, 3, 8, 13, 0, tzinfo=UTC))

    def test_spring_forward_skipped_time_moves_forward(self):
        now = datetime(2026, 3, 8, 0, 30, tzinfo=NY)
        s = self.ny("X at 2:30am", now)
        self.assertEqual(s.first_at.astimezone(NY), datetime(2026, 3, 8, 3, 30, tzinfo=NY))

    def test_fall_back_rollover_uses_new_offset(self):
        now = datetime(2026, 10, 31, 20, 0, tzinfo=NY)  # EDT
        s = self.ny("X at 8am", now)
        # 8am EST (UTC-5) = 13:00 UTC.
        self.assertEqual(s.first_at, datetime(2026, 11, 1, 13, 0, tzinfo=UTC))

    def test_fall_back_repeated_time_uses_first_occurrence(self):
        now = datetime(2026, 11, 1, 0, 30, tzinfo=NY)  # EDT
        s = self.ny("X at 1:30am", now)
        self.assertEqual(s.first_at, datetime(2026, 11, 1, 5, 30, tzinfo=UTC))  # 01:30 EDT

    def test_in_duration_is_elapsed_time(self):
        now = datetime(2026, 3, 8, 1, 30, tzinfo=NY)  # 30 min before the jump
        s = self.ny("X in 1h", now)
        self.assertEqual(s.first_at.astimezone(NY), datetime(2026, 3, 8, 3, 30, tzinfo=NY))


class BadInputTest(unittest.TestCase):
    def assertError(self, text):
        e = parse(text)
        self.assertIsInstance(e, p.ParseError, e)
        self.assertTrue(e.message)
        self.assertTrue(e.example.startswith("/snooze "))
        return e

    def test_empty(self):
        self.assertError("")
        self.assertError("   ")

    def test_no_time(self):
        e = self.assertError("Send the email")
        self.assertIn("at <time>", e.message)

    def test_no_text(self):
        self.assertError("at 4pm")
        self.assertError("at 4pm every 10m")
        self.assertError("8am 10m")

    def test_invalid_time_is_not_a_clause(self):
        self.assertError("Wake up at 25:00")
        self.assertError("Wake up at 8")
        self.assertError("Wake up at 13pm")
        self.assertError("Wake up at 8:75")

    def test_every_without_start(self):
        self.assertError("Take pill every 5m")

    def test_keyword_without_argument(self):
        self.assertError("Wake up at")
        self.assertError("Wake up at 8am every")

    def test_then_without_action(self):
        e = self.assertError("Wake up at 8am then")
        self.assertIn("then", e.message)

    def test_ok_contains_empty(self):
        self.assertError("Wake up at 8am ok contains:")

    def test_ok_punctuation_only(self):
        self.assertError("Wake up at 8am ok !!")

    def test_every_too_short(self):
        self.assertError("Wake up at 8am every 30s")

    def test_naive_now_raises(self):
        with self.assertRaises(ValueError):
            p.parse("X at 4pm", datetime(2026, 11, 7, 10, 0), PARIS)

    def test_error_str_has_example(self):
        e = parse("")
        self.assertIn(e.example, str(e))


if __name__ == "__main__":
    unittest.main()
