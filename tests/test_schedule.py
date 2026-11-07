import unittest
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from snooze import schedule
from snooze.schedule import Action
from snooze.store import ACTIVE, CANCELLED, DONE, MISSED, PENDING, Schedule, Snooze, Target

NOW = datetime(2026, 11, 7, 10, 0, tzinfo=timezone.utc)
CAP = 10


def make(state=ACTIVE, next_at=NOW, every=10, until=None, sent_count=0, created=NOW):
    return Snooze(
        text="Stretch",
        created=created.isoformat(),
        target=Target("telegram", "1", "1"),
        schedule=Schedule(first_at=NOW.isoformat(), every=every, until=until.isoformat() if until else None),
        state=state,
        sent_count=sent_count,
        next_at=next_at.isoformat() if next_at else None,
    )


class DueTest(unittest.TestCase):
    def test_send_on_time(self):
        self.assertEqual(schedule.due(make(), NOW, CAP), Action("send"))

    def test_send_within_grace_is_not_late(self):
        action = schedule.due(make(), NOW + schedule.LATE_GRACE, CAP)
        self.assertEqual(action, Action("send", None))

    def test_nothing_before_next_at(self):
        self.assertEqual(schedule.due(make(), NOW - timedelta(seconds=1), CAP), Action("nothing"))

    def test_nothing_without_next_at(self):
        self.assertEqual(schedule.due(make(next_at=None), NOW, CAP), Action("nothing"))

    def test_only_active_goes_on(self):
        for state in (DONE, MISSED, CANCELLED):
            with self.subTest(state=state):
                self.assertEqual(schedule.due(make(state=state), NOW, CAP), Action("nothing"))

    def test_until_reached_is_a_miss(self):
        s = make(until=NOW + timedelta(minutes=5))
        self.assertEqual(schedule.due(s, NOW + timedelta(minutes=4), CAP).kind, "send")
        self.assertEqual(schedule.due(s, NOW + timedelta(minutes=5), CAP), Action("miss"))

    def test_until_is_checked_before_next_at(self):
        s = make(next_at=NOW + timedelta(hours=1), until=NOW + timedelta(minutes=5))
        self.assertEqual(schedule.due(s, NOW + timedelta(minutes=5), CAP), Action("miss"))

    def test_cap_reached_misses_only_at_the_next_next_at(self):
        s = make(sent_count=CAP, next_at=NOW + timedelta(minutes=10))
        self.assertEqual(schedule.due(s, NOW + timedelta(minutes=9), CAP), Action("nothing"))
        self.assertEqual(schedule.due(s, NOW + timedelta(minutes=10), CAP), Action("miss"))

    def test_last_reminder_under_the_cap_is_sent(self):
        self.assertEqual(schedule.due(make(sent_count=CAP - 1), NOW, CAP).kind, "send")

    def test_late_by_after_a_long_sleep(self):
        action = schedule.due(make(), NOW + timedelta(hours=3), CAP)
        self.assertEqual(action, Action("send", timedelta(hours=3)))

    def test_pending_expired(self):
        s = make(state=PENDING, next_at=None)
        self.assertEqual(schedule.due(s, NOW + timedelta(minutes=10), CAP), Action("expire"))

    def test_pending_waiting(self):
        s = make(state=PENDING, next_at=None)
        self.assertEqual(schedule.due(s, NOW + timedelta(minutes=9), CAP), Action("nothing"))


class AfterSendTest(unittest.TestCase):
    def test_records_the_send_and_schedules_from_it(self):
        s = make(sent_count=2)
        s.last_error = "boom"
        sent_at = NOW + timedelta(minutes=1)
        schedule.after_send(s, sent_at)
        self.assertEqual(s.sent_count, 3)
        self.assertEqual(s.last_sent_at, sent_at.isoformat())
        self.assertEqual(s.next_at, (sent_at + timedelta(minutes=10)).isoformat())
        self.assertIsNone(s.last_error)

    def test_sleep_gives_one_late_reminder_then_every_after(self):
        s = make(every=10)
        wake = NOW + timedelta(hours=3)
        self.assertEqual(schedule.due(s, wake, CAP).late_by, timedelta(hours=3))
        schedule.after_send(s, wake)
        self.assertEqual(schedule.due(s, wake + timedelta(minutes=9), CAP), Action("nothing"))
        self.assertEqual(schedule.due(s, wake + timedelta(minutes=10), CAP), Action("send"))

    def test_fractional_every(self):
        s = make(every=1.5)
        schedule.after_send(s, NOW)
        self.assertEqual(s.next_at, (NOW + timedelta(seconds=90)).isoformat())

    def test_every_hour_across_the_paris_dst_change(self):
        paris = ZoneInfo("Europe/Paris")
        before = datetime(2026, 10, 25, 1, 30, tzinfo=timezone.utc).astimezone(
            paris
        )  # 03:30 CEST, the clocks go back at 03:00
        s = make(every=60, next_at=before)
        schedule.after_send(s, before)
        nxt = datetime.fromisoformat(s.next_at)
        self.assertEqual(nxt - before, timedelta(minutes=60))
        self.assertEqual(f"{nxt.astimezone(paris):%H:%M}", "03:30")  # same wall clock, 60 real minutes later
        # Spring forward: 01:30 CET + 60 real minutes is 03:30 CEST.
        spring = datetime(2026, 3, 29, 0, 30, tzinfo=timezone.utc).astimezone(paris)
        s = make(every=60, next_at=spring)
        schedule.after_send(s, spring)
        nxt = datetime.fromisoformat(s.next_at)
        self.assertEqual(nxt - spring, timedelta(minutes=60))
        self.assertEqual(f"{nxt.astimezone(paris):%H:%M}", "03:30")


class ApplyLaterTest(unittest.TestCase):
    def test_moves_next_at(self):
        s = make()
        at = schedule.apply_later(s, NOW, 30)
        self.assertEqual(at, NOW + timedelta(minutes=30))
        self.assertEqual(s.next_at, at.isoformat())

    def test_until_unchanged_when_still_after(self):
        until = NOW + timedelta(hours=1)
        s = make(until=until)
        schedule.apply_later(s, NOW, 30)
        self.assertEqual(s.schedule.until, until.isoformat())

    def test_later_past_until_extends_until(self):
        s = make(until=NOW + timedelta(minutes=10))
        at = schedule.apply_later(s, NOW, 30)
        self.assertEqual(s.schedule.until, (at + timedelta(minutes=10)).isoformat())
        self.assertEqual(schedule.due(s, at, CAP).kind, "send")  # one more reminder
        schedule.after_send(s, at)
        self.assertEqual(schedule.due(s, at + timedelta(minutes=10), CAP).kind, "miss")

    def test_no_until_stays_none(self):
        s = make()
        schedule.apply_later(s, NOW, 30)
        self.assertIsNone(s.schedule.until)

    def test_before_first_reminder_pushes_from_that_reminder(self):
        # first reminder at 10:26 (next_at, nothing sent), `later 10` at 09:56 -> 10:36, not 10:06
        s = make(next_at=NOW + timedelta(minutes=26))
        at = schedule.apply_later(s, NOW - timedelta(minutes=4), 10)
        self.assertEqual(at, NOW + timedelta(minutes=36))
        self.assertEqual(s.next_at, at.isoformat())

    def test_after_a_reminder_pushes_from_now(self):
        # reminder sent at 10:00, next at 10:10; `later 5` at 10:01 -> 10:06, not 10:15
        s = make(next_at=NOW + timedelta(minutes=10), sent_count=1)
        at = schedule.apply_later(s, NOW + timedelta(minutes=1), 5)
        self.assertEqual(at, NOW + timedelta(minutes=6))
        self.assertEqual(s.next_at, at.isoformat())

    def test_overdue_reminder_pushes_from_now(self):
        s = make(next_at=NOW - timedelta(minutes=3))
        at = schedule.apply_later(s, NOW, 10)
        self.assertEqual(at, NOW + timedelta(minutes=10))

    def test_no_next_at_pushes_from_now(self):
        s = make(next_at=None)
        self.assertEqual(schedule.apply_later(s, NOW, 10), NOW + timedelta(minutes=10))


class PendingExpiryTest(unittest.TestCase):
    def snooze(self, state=PENDING, created=NOW):
        return make(state=state, created=created, next_at=None)

    def test_deadline_is_created_plus_ten_minutes(self):
        self.assertEqual(schedule.pending_deadline(self.snooze()), NOW + timedelta(minutes=10))

    def test_not_expired_before_the_deadline(self):
        s = self.snooze()
        self.assertFalse(schedule.is_pending_expired(s, NOW))
        self.assertFalse(schedule.is_pending_expired(s, NOW + timedelta(minutes=9, seconds=59)))

    def test_expired_at_the_deadline(self):
        self.assertTrue(schedule.is_pending_expired(self.snooze(), NOW + timedelta(minutes=10)))

    def test_only_pending_snoozes_expire(self):
        late = NOW + timedelta(hours=5)
        for state in (ACTIVE, DONE, MISSED, CANCELLED):
            with self.subTest(state=state):
                self.assertFalse(schedule.is_pending_expired(self.snooze(state=state), late))

    def test_unreadable_created_never_expires(self):
        s = self.snooze()
        s.created = "garbage"
        self.assertIsNone(schedule.pending_deadline(s))
        self.assertFalse(schedule.is_pending_expired(s, NOW + timedelta(days=1)))


if __name__ == "__main__":
    unittest.main()
