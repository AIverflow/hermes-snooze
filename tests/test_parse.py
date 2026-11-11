import dataclasses
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


class LongDurationUnitsTest(unittest.TestCase):
    def assert_delay(self, text, expected):
        s = parse(text)
        self.assertIsInstance(s, p.Spec, s)
        self.assertEqual(s.text, text.split(" in ")[0])
        self.assertEqual(s.first_at, NOW.astimezone(UTC) + expected)

    def test_hour_with_and_without_space(self):
        self.assert_delay("Call mom in 1 hour", timedelta(hours=1))
        self.assert_delay("Call mom in 1hour", timedelta(hours=1))

    def test_long_units_in_combined_schedule(self):
        s = parse("Pill in 30 minutes every 5 minutes")
        self.assertEqual(s.first_at, NOW.astimezone(UTC) + timedelta(minutes=30))
        self.assertEqual(s.every, timedelta(minutes=5))

        s = parse("Stretch in 2 hours every 1 hour")
        self.assertEqual(s.first_at, NOW.astimezone(UTC) + timedelta(hours=2))
        self.assertEqual(s.every, timedelta(minutes=60))

    def test_compound_and_seconds(self):
        self.assert_delay("Tea in 1 hour 30 minutes", timedelta(minutes=90))
        self.assert_delay("Tea in 90 seconds", timedelta(seconds=90))

    def test_long_units_in_short_form(self):
        s = parse("Wake up 8am 10 minutes")
        self.assertEqual(s.first_at, paris(8, day=8))
        self.assertEqual(s.every, timedelta(minutes=10))

    def test_case_insensitive_long_unit(self):
        self.assert_delay("Call mom in 1 Hour", timedelta(hours=1))

    def test_seconds_below_minimum_remain_refused(self):
        error = parse("Call mom in 30 seconds")
        self.assertIsInstance(error, p.ParseError)
        self.assertIn("too short", error.message)


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

    def test_any_and_contains_are_plain_words(self):
        self.assertEqual(self.confirm("ok any"), p.ConfirmRule("word", "any"))
        self.assertEqual(self.confirm("ok contains:sent"), p.ConfirmRule("word", "contains:sent"))

    def test_emoji(self):
        self.assertEqual(self.confirm("ok \U0001f44d"), p.ConfirmRule("emoji", "\U0001f44d"))

    def test_emoji_with_variation_selector(self):
        self.assertEqual(self.confirm("ok ✔️"), p.ConfirmRule("emoji", "✔️"))

    def test_choice_keeps_exact_value(self):
        self.assertEqual(self.confirm("ok asian|French|italian"), p.ConfirmRule("choice", "asian|French|italian"))

    def test_choice_invalid_options(self):
        for arg in ("a|", "a|b|c|d|e|f|g|h|i|j", "a||b", "A|a", "|", "ship|ship!", "Ship|ship ", "a-b|a b"):
            with self.subTest(arg=arg):
                result = p.parse_ok_arg(arg)
                self.assertIsInstance(result, p.ParseError)
                self.assertTrue(result.example.startswith("/snooze "))

    def test_choice_with_other_clauses(self):
        s = parse("Dinner at 8pm every 10m ok asian|french then order it")
        self.assertIsInstance(s, p.Spec)
        self.assertEqual(s.text, "Dinner")
        self.assertEqual(s.every, timedelta(minutes=10))
        self.assertEqual(s.confirm, p.ConfirmRule("choice", "asian|french"))
        self.assertEqual(s.then, "order it")


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

    def test_at_and_in_together_are_refused(self):
        self.assertIsInstance(parse("Meet at 5pm in 10m"), p.ParseError)


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

    def test_too_large_duration_is_error_not_crash(self):
        for text in (
            "Backup in 999999999999999h",
            "Backup at 4pm every 999999999999999h",
            "Backup 8am 999999999999999h",
            "Backup in 99999999h",
            "Backup in " + "9" * 5000 + "h",
        ):
            with self.subTest(text=text):
                e = parse(text)
                self.assertIsInstance(e, p.ParseError)
                self.assertIn("too long", e.message)
                self.assertTrue(e.example.startswith("/snooze "))

    def test_in_below_one_minute_is_error(self):
        self.assertIsInstance(parse("X in 30s"), p.ParseError)

    def test_short_form_below_one_minute_is_error(self):
        self.assertIsInstance(parse("Wake up 8am 30s"), p.ParseError)

    def test_in_hours(self):
        s = parse("X in 2h")
        self.assertEqual(s.first_at, NOW.astimezone(UTC) + timedelta(hours=2))
        self.assertIsNone(s.every)


class SpacedDurationTest(unittest.TestCase):
    """A duration written as a number word then a unit word: `2 min`."""

    def test_live_sentence(self):
        s = parse("make a break at 8pm every 2 min until 10pm then turn all light on")
        self.assertIsInstance(s, p.Spec, s)
        self.assertEqual(s.text, "make a break")
        self.assertEqual(s.first_at, paris(20))
        self.assertEqual(s.every, timedelta(minutes=2))
        self.assertEqual(s.until, paris(22))
        self.assertEqual(s.then, "turn all light on")

    def test_in(self):
        s = parse("X in 5 min")
        self.assertEqual(s.text, "X")
        self.assertEqual(s.first_at, NOW.astimezone(UTC) + timedelta(minutes=5))

    def test_units(self):
        for d, expected in (("1 h", 60), ("30 sec", 0.5), ("2 MIN", 2), ("1 hr", 60)):
            with self.subTest(d=d):
                s = parse(f"X at 4pm every {d}")
                if expected < 1:
                    self.assertIsInstance(s, p.ParseError)
                else:
                    self.assertEqual(s.every, timedelta(minutes=expected))

    def test_below_one_minute_is_error(self):
        e = parse("X at 4pm every 30 sec")
        self.assertIsInstance(e, p.ParseError)
        self.assertIn("1 minute", e.message)

    def test_compact_forms_still_work(self):
        self.assertEqual(parse("X at 4pm every 2min").every, timedelta(minutes=2))
        self.assertEqual(parse("X at 4pm every 1h30m").every, timedelta(minutes=90))

    def test_short_form(self):
        s = parse("Wake up 8am 10 min")
        self.assertEqual(s.text, "Wake up")
        self.assertEqual(s.every, timedelta(minutes=10))

    def test_number_without_unit_stays_in_text(self):
        s = parse("Take 2 pills at 8am")
        self.assertEqual(s.text, "Take 2 pills")


class RecurrenceTest(unittest.TestCase):
    def test_parse_days_lists_ranges_and_case(self):
        self.assertEqual(p.parse_days("mon,thu"), frozenset({0, 3}))
        self.assertEqual(p.parse_days("mon-wed,fri"), frozenset({0, 1, 2, 4}))
        self.assertEqual(p.parse_days("MON-FRI"), frozenset({0, 1, 2, 3, 4}))
        self.assertIsNone(p.parse_days("fri-mon"))

    def test_daily_and_weekdays(self):
        daily = parse("Take pill at 8am daily")
        weekdays = parse("Take pill at 8am weekdays")
        self.assertEqual(daily.days, frozenset(range(7)))
        self.assertEqual(weekdays.days, frozenset(range(5)))

    def test_everyday_is_daily_in_either_position_and_any_case(self):
        for text in ("Take pill at 8am everyday", "Take pill everyday at 8am", "Take pill at 8am Everyday"):
            with self.subTest(text=text):
                s = parse(text)
                self.assertIsInstance(s, p.Spec)
                self.assertEqual(s.text, "Take pill")
                self.assertEqual(s.days, frozenset(range(7)))
                self.assertEqual(s, parse(text.replace("everyday", "daily").replace("Everyday", "daily")))

    def test_everyday_with_another_day_word_is_refused_like_daily(self):
        for text in ("Take pill at 8am everyday daily", "Take pill at 8am daily everyday"):
            with self.subTest(text=text):
                self.assertIsInstance(parse(text), p.ParseError)

    def test_second_everyday_before_the_time_stays_in_the_text(self):
        s = parse("Take pill everyday everyday at 8am")
        self.assertEqual(s.text, "Take pill everyday")
        self.assertEqual(s.days, frozenset(range(7)))

    def test_every_days(self):
        self.assertEqual(parse("Water plants at 8am every mon,thu").days, frozenset({0, 3}))
        self.assertEqual(parse("Water plants at 8am every mon-wed,fri").days, frozenset({0, 1, 2, 4}))
        self.assertEqual(parse("Water plants at 8am every MON-FRI").days, frozenset(range(5)))
        self.assertEqual(parse("Water plants at 8am every monday-friday").days, frozenset(range(5)))

    def test_every_day_and_every_interval_together(self):
        for text in (
            "Water the plants at 8am every monday every 10m",
            "Water the plants at 8am every 10m every monday",
            "Water the plants 8am 10m every monday",
        ):
            with self.subTest(text=text):
                s = parse(text)
                self.assertIsInstance(s, p.Spec)
                self.assertEqual(s.text, "Water the plants")
                self.assertEqual(s.days, frozenset({0}))
                self.assertEqual(s.every, timedelta(minutes=10))

    def test_every_days_keeps_the_interval_unset_and_combines_with_other_clauses(self):
        s = parse("Water the plants at 8am every monday until 9am ok done then close it")
        self.assertEqual(s.days, frozenset({0}))
        self.assertIsNone(s.every)
        self.assertEqual(s.confirm, p.ConfirmRule("word", "done"))
        self.assertEqual(s.then, "close it")
        self.assertEqual(s.until - s.first_at, timedelta(hours=1))

    def test_two_day_clauses_are_not_both_read(self):
        self.assertIsInstance(parse("X at 8am every monday every tuesday"), p.ParseError)
        self.assertIsInstance(parse("X at 8am daily every monday"), p.ParseError)

    def test_on_day_is_one_time_on_the_next_such_day(self):
        s = parse("Call the dentist on monday at 9am")
        self.assertIsInstance(s, p.Spec)
        self.assertEqual(s.text, "Call the dentist")
        self.assertIsNone(s.days)
        self.assertEqual(s.first_at, paris(9, day=9))
        self.assertEqual(parse("Call the dentist at 9am on Mon"), s)
        self.assertEqual(parse("Call the dentist 9am on monday"), s)

    def test_on_day_today_if_still_ahead_else_next_week(self):
        # NOW is Saturday 10:00.
        self.assertEqual(parse("Call the dentist at 11am on saturday").first_at, paris(11))
        self.assertEqual(parse("Call the dentist at 9am on saturday").first_at, paris(9, day=14))

    def test_on_several_days_is_refused_with_an_every_example(self):
        for text in ("X at 8am on monday,thursday", "X at 8am on monday-friday"):
            with self.subTest(text=text):
                result = parse(text)
                self.assertIsInstance(result, p.ParseError)
                self.assertIn("every monday,thursday", result.example)

    def test_second_days_clause_and_every_day_get_a_plain_error(self):
        for text, part in (
            ("X at 8am every monday every tuesday", "`every <days>` once"),
            ("X at 8am every day", "`daily`"),
            ("X at 8am every weekday", "`weekdays`"),
        ):
            with self.subTest(text=text):
                result = parse(text)
                self.assertIsInstance(result, p.ParseError)
                self.assertIn(part, result.message)

    def test_on_day_with_in_is_an_error(self):
        result = parse("Call the dentist in 1h on monday")
        self.assertIsInstance(result, p.ParseError)
        self.assertIn("`in`", result.message)

    def test_invalid_range_stays_in_text(self):
        s = parse("Take pill at 8am on fri-mon")
        self.assertIsInstance(s, p.ParseError)
        self.assertIn("`on fri-mon`", s.message)

    def test_on_shelf_stays_in_text(self):
        s = parse("Put the book on shelf at 8am")
        self.assertEqual(s.text, "Put the book on shelf")
        self.assertIsNone(s.days)

    def test_full_and_short_day_names(self):
        for text, days in (
            ("Water the plants at 8am every monday", {0}),
            ("Water the plants at 8am every Monday,thursday", {0, 3}),
            ("Water the plants at 8am every mon,thursday", {0, 3}),
            ("Water the plants at 8am every monday-friday", {0, 1, 2, 3, 4}),
            ("Water the plants at 8am every mon-friday", {0, 1, 2, 3, 4}),
        ):
            with self.subTest(text=text):
                s = parse(text)
                self.assertIsInstance(s, p.Spec)
                self.assertEqual(s.text, "Water the plants")
                self.assertEqual(s.days, frozenset(days))

    def test_bad_day_names_are_refused(self):
        self.assertIsNone(p.parse_days("funday"))
        self.assertIsNone(p.parse_days("monday-sunday-tuesday"))
        self.assertIsNone(p.parse_days("friday-monday"))
        self.assertIsNone(p.parse_days("monday,"))
        self.assertIsInstance(parse("Water the plants at 8am on funday"), p.ParseError)

    def test_day_name_without_on_stays_in_text(self):
        s = parse("Prepare for Monday at 3pm")
        self.assertIsInstance(s, p.Spec)
        self.assertEqual(s.text, "Prepare for Monday")
        self.assertIsNone(s.days)

    def test_recurrence_with_in_is_an_error(self):
        result = parse("Take pill in 1h daily")
        self.assertIsInstance(result, p.ParseError)
        self.assertEqual(result.message, "A recurring snooze needs `at <time>`, not `in`.")
        self.assertIn("daily", result.example)

    def test_short_form_with_daily(self):
        s = parse("Wake up 8am 10m daily")
        self.assertEqual(s.text, "Wake up")
        self.assertEqual(s.every, timedelta(minutes=10))
        self.assertEqual(s.days, frozenset(range(7)))

    def test_weekdays_skips_saturday(self):
        s = parse("Wake up weekdays at 8am")
        self.assertEqual(s.first_at, paris(8, day=9))

    def test_daily_at_end_of_text_is_read_as_clause(self):
        s = parse("Read the daily at 8am")
        self.assertEqual(s.text, "Read the")
        self.assertEqual(s.days, frozenset(range(7)))
        self.assertIsNone(s.every)

    def test_number_unit_in_text_is_kept_as_written(self):
        s = parse("Run 5   min at 8am")
        self.assertEqual(s.text, "Run 5   min")
        self.assertIsNone(s.every)

    def test_hours_and_minutes_words(self):
        s = parse("X at 4pm every 1 h 30 min")
        self.assertEqual(s.text, "X")
        self.assertEqual(s.every, timedelta(minutes=90))


class ViaTest(unittest.TestCase):
    def test_via_platform_is_read_in_lowercase(self):
        for text in ("Water at 8am via discord", "Water at 8am via Discord", "Water via DISCORD at 8am"):
            with self.subTest(text=text):
                s = parse(text)
                self.assertEqual((s.text, s.platform), ("Water", "discord"))
        self.assertEqual(parse("Water at 8am via telegram").platform, "telegram")

    def test_no_via_means_no_platform(self):
        self.assertIsNone(parse("Water at 8am").platform)

    def test_unknown_platform_leaves_via_in_the_text(self):
        s = parse("Go to Lyon via Dijon at 8am")
        self.assertEqual((s.text, s.platform), ("Go to Lyon via Dijon", None))
        self.assertIsInstance(parse("Go to Lyon at 8am via Dijon"), p.ParseError)  # a stray tail, like `please`

    def test_text_ending_in_a_platform_name_without_via_is_text(self):
        s = parse("Post the release notes to discord at 8am")
        self.assertEqual((s.text, s.platform), ("Post the release notes to discord", None))

    def test_via_twice_is_refused(self):
        for text in ("Water at 8am via discord via telegram", "Water at 8am via discord via discord"):
            with self.subTest(text=text):
                result = parse(text)
                self.assertIsInstance(result, p.ParseError)
                self.assertIn("`via` twice", result.message)

    def test_via_in_any_order_with_the_other_clauses(self):
        base = parse("Water the plants at 8am every monday every 10m until 9am ok done")
        for text in (
            "Water the plants via discord at 8am every monday every 10m until 9am ok done",
            "Water the plants at 8am via discord every monday every 10m until 9am ok done",
            "Water the plants at 8am every monday every 10m until 9am ok done via discord",
            "Water the plants at 8am every monday ok done every 10m via discord until 9am",
            "Water the plants at 8am via discord ok done until 9am every 10m every monday",
        ):
            with self.subTest(text=text):
                s = parse(text)
                self.assertEqual(s, dataclasses.replace(base, platform="discord"))

    def test_via_with_on_day_daily_and_short_form(self):
        self.assertEqual(parse("Call at 9am on monday via discord").platform, "discord")
        self.assertEqual(parse("Call at 9am via discord daily").days, frozenset(range(7)))
        s = parse("Wake up 8am 10m via discord")
        self.assertEqual((s.text, s.every, s.platform), ("Wake up", timedelta(minutes=10), "discord"))
        s = parse("Wake up in 30m via discord ok")
        self.assertEqual((s.text, s.platform), ("Wake up", "discord"))

    def test_via_before_then_and_then_keeps_its_text(self):
        s = parse("Send the report at 4pm via discord then post it via telegram")
        self.assertEqual((s.text, s.platform), ("Send the report", "discord"))
        self.assertEqual(s.then, "post it via telegram")

    def test_via_alone_is_missing_text(self):
        self.assertIsInstance(parse("via discord at 8am"), p.ParseError)


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


class PreciseErrorTest(unittest.TestCase):
    """Errors name the clause in fault: missing start, bad duration, unknown suffix, two starts."""

    def assertErrorAbout(self, text, *fragments):
        e = parse(text)
        self.assertIsInstance(e, p.ParseError, e)
        for fragment in fragments:
            self.assertIn(fragment, e.message)
        self.assertTrue(e.example.startswith("/snooze "))
        # The example is a valid command.
        self.assertIsInstance(parse(e.example.removeprefix("/snooze ")), p.Spec, e.example)
        return e

    def test_missing_start_stays_generic(self):
        for text in ("Send the report", "Take pill every 5m", "Meet me in Paris", "Call mom before 6pm"):
            with self.subTest(text=text):
                self.assertErrorAbout(text, "Missing when to start")

    def test_trailing_word_after_every_is_an_unrecognized_suffix(self):
        e = self.assertErrorAbout("Send report at 16:00 every 10m please", "`please`", "`every 10m`")
        self.assertNotIn("Missing when to start", e.message)

    def test_trailing_word_after_at_is_an_unrecognized_suffix(self):
        self.assertErrorAbout("Send report at 16:00 please", "`please`", "`at 16:00`")

    def test_several_trailing_words_are_all_named(self):
        self.assertErrorAbout("Send report at 16:00 thanks a lot", "`thanks a lot`", "`at 16:00`")

    def test_suffix_after_in(self):
        self.assertErrorAbout("Take pill in 30m please", "`please`", "`in 30m`")

    def test_spoken_duration_is_an_invalid_duration(self):
        e = self.assertErrorAbout("Send report at 16:00 every ten minutes", "`every ten minutes`", "duration")
        self.assertNotIn("Missing when to start", e.message)

    def test_unknown_unit_is_an_invalid_duration(self):
        self.assertErrorAbout("Send report at 16:00 every 10x", "`every 10x`", "duration")
        self.assertErrorAbout("Take pill in 5 pizzas", "`in 5 pizzas`", "duration")

    def test_invalid_time_is_named(self):
        self.assertErrorAbout("Wake up at 25:00", "`at 25:00`", "time")
        self.assertErrorAbout("Wake up at 8am until 99:00", "`until 99:00`", "time")

    def test_keyword_without_a_value(self):
        self.assertErrorAbout("Wake up at 8am every", "`every`")
        self.assertErrorAbout("Wake up at", "`at`")

    def test_duration_kinds_are_told_apart(self):
        bad = parse("Send report at 16:00 every 10x")
        suffix = parse("Send report at 16:00 every 10m please")
        missing = parse("Send report")
        two = parse("Send report at 16:00 in 30m")
        messages = {e.message for e in (bad, suffix, missing, two)}
        self.assertEqual(len(messages), 4)

    def test_at_then_in_is_contradictory(self):
        e = self.assertErrorAbout("Send report at 16:00 in 30m", "`at 16:00`", "`in 30m`")
        self.assertIn("Two start times", e.message)

    def test_in_then_at_is_contradictory(self):
        self.assertErrorAbout("Send report in 30m at 16:00", "Two start times", "`at 16:00`", "`in 30m`")

    def test_contradictory_starts_with_other_clauses(self):
        self.assertErrorAbout("Send report at 16:00 in 30m every 5m until 18:00", "Two start times")
        self.assertErrorAbout("Send report at 16:00 in 30m ok done", "Two start times")

    def test_contradictory_starts_before_then_are_refused_but_then_is_opaque(self):
        self.assertErrorAbout("Send report at 16:00 in 30m then ping", "Two start times")
        s = parse("Send report at 16:00 then check at 17:00 in 30m please")
        self.assertIsInstance(s, p.Spec)
        self.assertEqual(s.then, "check at 17:00 in 30m please")

    def test_legit_free_text_still_parses(self):
        cases = {
            "Call mom before 6pm at 4pm": "Call mom before 6pm",
            "Meet me in Paris at 4pm": "Meet me in Paris",
            "Ask what happens at the end in the morning at 4pm": "Ask what happens at the end in the morning",
            "Dinner at 7pm at 6pm": "Dinner at 7pm",
            "Water the plants every day in 10m": "Water the plants every day",
            "Reply please at 4pm": "Reply please",
        }
        for text, expected in cases.items():
            with self.subTest(text=text):
                s = parse(text)
                self.assertIsInstance(s, p.Spec, s)
                self.assertEqual(s.text, expected)


if __name__ == "__main__":
    unittest.main()
