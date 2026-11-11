import asyncio
import contextvars
import inspect
import json
import logging
import sys
import tempfile
import threading
import types
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from snooze import schedule, send
from snooze.store import ACTIVE, DONE, MISSED, PENDING, Schedule, SenderLock, Snooze, Store, Target

T0 = datetime(2026, 11, 7, 17, 0, tzinfo=timezone.utc)


def iso(dt):
    return dt.isoformat()


def make(*, state=ACTIVE, every=10, until=T0 + timedelta(hours=1), next_at=T0, schedule=None, **kw):
    return Snooze(
        text="Take the pill",
        created=iso(T0 - timedelta(hours=1)),
        target=Target(platform="telegram", chat_id="123"),
        schedule=schedule or Schedule(first_at=iso(T0), every=every, until=iso(until) if until else None),
        state=state,
        next_at=iso(next_at),
        **kw,
    )


class FakeSender:
    def __init__(self):
        self.sent = []  # (platform, chat_id, text)
        self.fail = None  # an error text: send raises SendError with it
        self.during = None  # called inside send(), to act "while the message is on the wire"

    def send(self, target, text):
        if self.during:
            self.during()
        if self.fail:
            raise send.SendError(self.fail)
        self.sent.append((target.platform, target.chat_id, text))
        return f"m{len(self.sent)}"


class TimerCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.store = Store(self.dir, lock_timeout=0.5)
        self.sender = FakeSender()
        self.now = T0
        self.paused = False
        self.timer = self.make_timer()
        logger = logging.getLogger("snooze.send")
        level = logger.level
        logger.setLevel(logging.CRITICAL)  # the failure tests would otherwise print warnings
        self.addCleanup(logger.setLevel, level)

    def make_timer(self, sender=None):
        timer = send.Timer(
            self.store,
            sender or self.sender,
            SenderLock(self.dir),
            cap_reminders=lambda: 3,
            now=lambda: self.now,
            is_paused=lambda: self.paused,
        )
        self.addCleanup(timer.lock.release)
        return timer

    def add(self, **kw):
        return self.store.add(make(**kw)).id

    def get(self, snooze_id):
        return self.store.get(snooze_id)


class TickTest(TimerCase):
    def test_sends_a_due_reminder_and_schedules_the_next(self):
        sid = self.add()
        self.assertEqual(self.timer.tick(), 1)
        self.assertEqual(self.sender.sent, [("telegram", "123", '🔔 Snooze #1: Take the pill  (reply "Ok" / later)')])
        s = self.get(sid)
        self.assertEqual((s.state, s.sent_count, s.last_message_id), (ACTIVE, 1, "m1"))
        self.assertEqual(s.last_sent_at, iso(T0))
        self.assertEqual(s.next_at, iso(T0 + timedelta(minutes=10)))

    def test_nothing_due_sends_nothing_and_writes_nothing(self):
        self.add(next_at=T0 + timedelta(minutes=5))
        before = self.store.path.read_text()
        self.assertEqual(self.timer.tick(), 0)
        self.assertEqual(self.sender.sent, [])
        self.assertEqual(self.store.path.read_text(), before)

    def test_second_reminder_says_its_number(self):
        self.add()
        self.timer.tick()
        self.now = T0 + timedelta(minutes=10)
        self.timer.tick()
        self.assertEqual(self.sender.sent[1][2], "🔔 Snooze #1: Take the pill (2nd)")

    def test_late_reminder_says_how_late(self):
        self.add()
        self.now = T0 + timedelta(minutes=40)
        self.timer.tick()
        self.assertTrue(self.sender.sent[0][2].endswith("⏰ late by 40 min"))

    def test_the_target_is_a_platform_chat_pair(self):
        self.add()
        s = make()
        s.target = Target(platform="discord", chat_id="999")
        self.store.add(s)
        self.timer.tick()
        self.assertEqual({(p, c) for p, c, _ in self.sender.sent}, {("telegram", "123"), ("discord", "999")})

    def test_cap_ends_the_snooze_with_a_missed_message(self):
        sid = self.add(sent_count=3)
        self.timer.tick()
        self.assertEqual(self.sender.sent, [("telegram", "123", "⌛ Missed: Take the pill (3 reminders, no answer)")])
        self.assertEqual(self.get(sid).state, MISSED)

    def test_recurring_cap_sends_missed_text_and_rearms_for_next_day(self):
        sid = self.add(
            sent_count=3,
            schedule=Schedule(
                first_at=iso(T0),
                every=10,
                until=iso(T0 + timedelta(hours=2)),
                tz="Europe/Paris",
                days=list(range(7)),
                span=120,
            ),
            next_at=T0,
        )
        self.timer.tick()
        self.assertEqual(
            self.sender.sent[0][2],
            "⌛ Missed: Take the pill (3 reminders, no answer)\nNext: tomorrow 18:00",
        )
        s = self.get(sid)
        self.assertEqual((s.state, s.sent_count, s.next_at), (ACTIVE, 0, iso(T0 + timedelta(days=1))))

    def test_expired_pending_recurring_snooze_does_not_rearm(self):
        sid = self.add(
            state=PENDING,
            schedule=Schedule(first_at=iso(T0), every=10, tz="Europe/Paris", days=list(range(7)), span=120),
        )
        self.now = T0 + timedelta(hours=2)
        self.timer.tick()
        self.assertEqual(self.get(sid).state, MISSED)

    def test_miss_message_id_does_not_replace_last_reminder_id(self):
        sid = self.add(sent_count=3, last_message_id="last-reminder")
        self.timer.tick()
        self.assertEqual(self.get(sid).last_message_id, "last-reminder")

    def test_until_ends_the_snooze(self):
        sid = self.add(until=T0, sent_count=1)
        self.timer.tick()
        self.assertEqual(self.sender.sent[0][2], "⌛ Missed: Take the pill (1 reminder, no answer)")
        self.assertEqual(self.get(sid).state, MISSED)
        self.assertEqual(self.timer.tick(), 0)  # said once

    def test_pending_snooze_expires_with_not_started(self):
        sid = self.add(state=PENDING, next_at=T0)
        self.now = T0 + timedelta(hours=2)
        self.timer.tick()
        self.assertEqual(self.sender.sent, [("telegram", "123", "⌛ Not started: Take the pill")])
        self.assertEqual(self.get(sid).state, MISSED)

    def test_done_and_cancelled_snoozes_are_left_alone(self):
        self.add(state=DONE)
        self.assertEqual(self.timer.tick(), 0)


class FailureTest(TimerCase):
    def test_failed_send_stores_last_error_and_retries(self):
        sid = self.add()
        self.sender.fail = "chat not found"
        self.assertEqual(self.timer.tick(), 0)
        s = self.get(sid)
        self.assertEqual((s.sent_count, s.last_error, s.next_at), (0, "chat not found", iso(T0)))
        self.sender.fail = None
        self.now = T0 + timedelta(seconds=15)
        self.assertEqual(self.timer.tick(), 1)
        s = self.get(sid)
        self.assertEqual((s.sent_count, s.last_error), (1, None))
        self.assertEqual(s.next_at, iso(self.now + timedelta(minutes=10)))

    def test_unexpected_exception_from_the_sender_is_an_error_too(self):
        sid = self.add()
        self.sender.send = mock.Mock(side_effect=ValueError("boom"))
        self.timer.tick()
        self.assertEqual(self.get(sid).last_error, "boom")

    def test_failed_miss_message_is_not_retried_and_keeps_the_error(self):
        sid = self.add(sent_count=3)
        self.sender.fail = "blocked"
        self.timer.tick()
        s = self.get(sid)
        self.assertEqual((s.state, s.last_error), (MISSED, "blocked"))
        self.sender.fail = None
        self.assertEqual(self.timer.tick(), 0)
        self.assertEqual(self.sender.sent, [])

    def test_long_error_is_cut(self):
        sid = self.add()
        self.sender.fail = "x" * 1000
        self.timer.tick()
        self.assertLessEqual(len(self.get(sid).last_error), send.MAX_ERROR_CHARS)

    def test_corrupt_store_raises_for_the_loop_to_log(self):
        self.store.path.write_text("{not json")
        with self.assertRaises(send.StoreError):
            self.timer.tick()


class LockTest(TimerCase):
    def test_two_timers_never_send_twice(self):
        self.add()
        other_sender = FakeSender()
        other = self.make_timer(other_sender)
        self.assertEqual(self.timer.tick(), 1)
        self.assertEqual(other.tick(), 0)
        self.now = T0 + timedelta(minutes=10)
        self.assertEqual(other.tick(), 0)  # still not its turn
        self.assertEqual(self.timer.tick(), 1)
        self.assertEqual(other_sender.sent, [])

    def test_another_timer_takes_over_when_the_holder_stops(self):
        self.add()
        other_sender = FakeSender()
        other = self.make_timer(other_sender)
        self.timer.tick()
        self.timer.lock.release()
        self.now = T0 + timedelta(minutes=10)
        self.assertEqual(other.tick(), 1)

    def test_store_is_not_locked_during_the_send(self):
        sid = self.add()
        outcome = []

        def during():
            # A command (here: a store write with a short lock timeout) must go through mid-send.
            self.store.update(lambda txn: txn.get(sid).__setattr__("text", "edited"))
            outcome.append("written")

        self.sender.during = during
        self.timer.tick()
        self.assertEqual(outcome, ["written"])
        self.assertEqual(self.get(sid).text, "edited")
        self.assertEqual(self.get(sid).sent_count, 1)

    def test_confirmed_during_the_send_stays_confirmed(self):
        sid = self.add()
        self.sender.during = lambda: self.store.update(lambda txn: setattr(txn.get(sid), "state", DONE))
        self.timer.tick()
        s = self.get(sid)
        self.assertEqual((s.state, s.sent_count), (DONE, 1))

    def test_recurring_answered_during_the_send_keeps_the_rearmed_occurrence(self):
        sid = self.add(
            schedule=Schedule(
                first_at=iso(T0), every=10, until=iso(T0 + timedelta(hours=1)), tz="UTC", days=list(range(7)), span=60
            ),
        )
        self.sender.during = lambda: self.store.update(lambda txn: schedule.rearm(txn.get(sid), self.now))
        self.timer.tick()
        s = self.get(sid)
        tomorrow = iso(T0 + timedelta(days=1))
        self.assertEqual((s.state, s.next_at, s.schedule.first_at), (ACTIVE, tomorrow, tomorrow))
        self.assertEqual((s.sent_count, s.last_sent_at, s.last_message_id), (0, None, None))

    def test_recurring_failed_send_after_the_rearm_leaves_no_error_on_the_next_occurrence(self):
        sid = self.add(
            schedule=Schedule(
                first_at=iso(T0), every=10, until=iso(T0 + timedelta(hours=1)), tz="UTC", days=list(range(7)), span=60
            ),
        )
        self.sender.fail = "chat not found"
        self.sender.during = lambda: self.store.update(lambda txn: schedule.rearm(txn.get(sid), self.now))
        self.timer.tick()
        self.assertIsNone(self.get(sid).last_error)

    def test_later_during_the_send_keeps_the_new_time(self):
        sid = self.add()
        pushed = iso(T0 + timedelta(minutes=30))
        self.sender.during = lambda: self.store.update(lambda txn: setattr(txn.get(sid), "next_at", pushed))
        self.timer.tick()
        s = self.get(sid)
        self.assertEqual((s.next_at, s.sent_count, s.last_message_id), (pushed, 1, "m1"))


class PauseTest(TimerCase):
    def test_paused_sends_nothing_then_one_catch_up_after_resume(self):
        sid = self.add()
        self.paused = True
        self.now = T0 + timedelta(minutes=45)
        self.assertEqual(self.timer.tick(), 0)
        self.assertEqual(self.sender.sent, [])
        self.paused = False
        self.assertEqual(self.timer.tick(), 1)
        self.assertEqual(self.timer.tick(), 0)  # one reminder, not a burst
        self.assertTrue(self.sender.sent[0][2].endswith("⏰ late by 45 min"))
        self.assertEqual(self.get(sid).sent_count, 1)

    def test_paused_holds_the_missed_message_too(self):
        sid = self.add(sent_count=3)
        self.paused = True
        self.timer.tick()
        self.assertEqual(self.get(sid).state, ACTIVE)
        self.assertEqual(self.sender.sent, [])

    def test_hermes_is_paused_reads_the_estop_switch(self):
        estop = types.ModuleType("agent.estop")
        estop.is_engaged = lambda: True
        with mock.patch.dict(sys.modules, {"agent": types.ModuleType("agent"), "agent.estop": estop}):
            self.assertTrue(send.hermes_is_paused())
            estop.is_engaged = lambda: False
            self.assertFalse(send.hermes_is_paused())

    def test_hermes_is_paused_is_false_without_hermes(self):
        with mock.patch.dict(sys.modules, {"agent": None}):
            self.assertFalse(send.hermes_is_paused())


class HermesSenderTest(unittest.TestCase):
    def sender_with(self, reply):
        module = types.ModuleType("tools.send_message_tool")
        module.send_message_tool = mock.Mock(side_effect=lambda args: reply(args))
        patch = mock.patch.dict(sys.modules, {"tools": types.ModuleType("tools"), "tools.send_message_tool": module})
        patch.start()
        self.addCleanup(patch.stop)
        return module

    def test_sends_to_platform_chat_and_returns_the_message_id(self):
        module = self.sender_with(lambda args: json.dumps({"success": True, "message_id": 77}))
        mid = send.HermesSender().send(Target(platform="slack", chat_id="C123"), "hello")
        self.assertEqual(mid, "77")
        module.send_message_tool.assert_called_once_with({"action": "send", "target": "slack:C123", "message": "hello"})

    def test_no_message_id_is_none(self):
        self.sender_with(lambda args: json.dumps({"success": True}))
        self.assertIsNone(send.HermesSender().send(Target(platform="signal", chat_id="+33"), "hi"))

    def test_error_reply_raises_send_error(self):
        self.sender_with(lambda args: json.dumps({"error": "Unknown platform"}))
        with self.assertRaisesRegex(send.SendError, "Unknown platform"):
            send.HermesSender().send(Target(platform="x", chat_id="1"), "hi")

    def test_exception_and_garbage_reply_raise_send_error(self):
        def boom(args):
            raise RuntimeError("network down")

        self.sender_with(boom)
        with self.assertRaisesRegex(send.SendError, "network down"):
            send.HermesSender().send(Target(platform="telegram", chat_id="1"), "hi")
        self.sender_with(lambda args: "not json")
        with self.assertRaises(send.SendError):
            send.HermesSender().send(Target(platform="telegram", chat_id="1"), "hi")


class FakeTask:
    def __init__(self):
        self._done = False

    def done(self):
        return self._done


class MayStartTest(unittest.TestCase):
    def test_only_without_a_live_task(self):
        live, finished = FakeTask(), FakeTask()
        finished._done = True
        self.assertTrue(send.may_start(None))
        self.assertTrue(send.may_start(finished))
        self.assertFalse(send.may_start(live))


class FakeLoop:
    def __init__(self, running=True, closed=False):
        self.running = running
        self.closed = closed

    def is_running(self):
        return self.running

    def is_closed(self):
        return self.closed


def in_loop(fn):
    """Call `fn()` from inside a running event loop (the thread `spawn_task` needs)."""

    async def main():
        return fn()

    return asyncio.run(main())


def join_waiters(timeout=5):
    """Wait for every `snooze-timer-wait` thread to end."""
    for thread in threading.enumerate():
        if thread.name == "snooze-timer-wait":
            thread.join(timeout)
            assert not thread.is_alive()


class StartTest(unittest.TestCase):
    class Ctx:
        def __init__(self, error=None):
            self.error = error
            self.spawned = []
            self.tasks = []

        def get_config(self, key, default=None):
            return default

        def spawn_task(self, coro, *, name=None):
            if self.error:
                raise self.error
            self.spawned.append((coro, name))
            coro.close()
            self.tasks.append(FakeTask())
            return self.tasks[-1]

    def setUp(self):
        send._timers.clear()
        self.addCleanup(send._timers.clear)
        self.hops = []  # (coro, loop) handed to run_coroutine_threadsafe
        self.tasks = []
        self.hop_error = None

    def gateway(self, running=True, loop=None):
        self.runner = runner = types.SimpleNamespace(_gateway_loop=loop)
        module = types.ModuleType("gateway.run")
        module._gateway_runner_ref = lambda: runner if running else None
        return mock.patch.dict(sys.modules, {"gateway": types.ModuleType("gateway"), "gateway.run": module})

    def data_dir(self, tmp):
        return mock.patch.object(send, "hermes_data_dir", lambda: tmp)

    def fake_hop(self):
        """Replace run_coroutine_threadsafe: record the call instead of touching a loop."""

        def hop(coro, loop):
            self.hops.append((coro, loop))
            if self.hop_error:
                raise self.hop_error  # the caller must close the coroutine
            coro.close()
            self.tasks.append(FakeTask())
            return self.tasks[-1]

        return mock.patch.object(send.asyncio, "run_coroutine_threadsafe", hop)

    def test_not_started_outside_the_gateway(self):
        ctx = self.Ctx()
        with mock.patch.dict(sys.modules, {"gateway.run": None}):
            self.assertFalse(send.start_timer(ctx))
        with self.gateway(running=False):
            self.assertFalse(send.start_timer(ctx))
        self.assertEqual(ctx.spawned, [])

    def test_a_running_loop_here_uses_spawn_task(self):
        ctx = self.Ctx()
        with tempfile.TemporaryDirectory() as tmp, self.gateway(), self.data_dir(tmp), self.fake_hop():
            self.assertTrue(in_loop(lambda: send.start_timer(ctx)))
        self.assertEqual([name for _, name in ctx.spawned], ["snooze:timer"])
        self.assertEqual(self.hops, [])

    def test_a_second_call_starts_nothing_while_the_task_lives(self):
        ctx = self.Ctx()
        with tempfile.TemporaryDirectory() as tmp, self.gateway(), self.data_dir(tmp):

            def scenario():
                self.assertTrue(send.start_timer(ctx))
                self.assertFalse(send.start_timer(ctx))
                self.assertEqual(len(ctx.spawned), 1)
                ctx.tasks[0]._done = True  # the task ended (plugin reloaded): a new one may start
                self.assertTrue(send.start_timer(ctx))
                self.assertEqual(len(ctx.spawned), 2)

            in_loop(scenario)

    def test_each_profile_gets_its_own_timer(self):
        ctx = self.Ctx()
        with tempfile.TemporaryDirectory() as a, tempfile.TemporaryDirectory() as b, self.gateway():

            def scenario():
                with self.data_dir(a):
                    self.assertTrue(send.start_timer(ctx))
                with self.data_dir(b):
                    self.assertTrue(send.start_timer(ctx))

            in_loop(scenario)
        self.assertEqual(len(ctx.spawned), 2)

    def test_no_loop_here_hops_onto_the_gateway_loop(self):
        ctx, loop = self.Ctx(), FakeLoop()
        with tempfile.TemporaryDirectory() as tmp, self.gateway(loop=loop), self.data_dir(tmp), self.fake_hop():
            with self.assertLogs("snooze.send", "INFO") as logs:
                self.assertTrue(send.start_timer(ctx))
        self.assertEqual([hop_loop for _, hop_loop in self.hops], [loop])
        self.assertEqual(ctx.spawned, [])
        self.assertIn("started on the gateway loop", logs.output[0])

    def test_a_second_call_does_not_hop_twice(self):
        ctx = self.Ctx()
        with tempfile.TemporaryDirectory() as tmp, self.gateway(loop=FakeLoop()), self.data_dir(tmp), self.fake_hop():
            self.assertTrue(send.start_timer(ctx))
            self.assertFalse(send.start_timer(ctx))
            self.assertEqual(len(self.hops), 1)
            self.tasks[0]._done = True
            self.assertTrue(send.start_timer(ctx))
            self.assertEqual(len(self.hops), 2)

    def test_a_loop_that_is_not_ready_is_logged_with_its_reason(self):
        cases = [
            (None, "_gateway_loop is None"),
            (FakeLoop(closed=True), "loop closed"),
            (FakeLoop(running=False), "loop not running"),
        ]
        for loop, reason in cases:
            with self.subTest(reason=reason):
                ctx = self.Ctx()
                with tempfile.TemporaryDirectory() as tmp, self.gateway(loop=loop), self.data_dir(tmp):
                    with self.fake_hop(), self.assertLogs("snooze.send", "INFO") as logs:
                        self.assertFalse(send.start_timer(ctx, wait_timeout=0.02, wait_poll=0.005))
                        join_waiters()
                self.assertIn(f"gateway loop not ready ({reason})", logs.output[0])
                self.assertTrue(any("waiting for the gateway loop" in line for line in logs.output))
                self.assertTrue(any("gave up" in line and reason in line for line in logs.output))
                self.assertEqual((ctx.spawned, self.hops, send._timers), ([], [], {}))
                send._timers.clear()

    def test_no_runner_is_a_reason_too(self):
        module = types.ModuleType("gateway.run")
        module._gateway_runner_ref = lambda: None
        with mock.patch.dict(sys.modules, {"gateway.run": module}):
            self.assertEqual(send._gateway_loop_state(), (None, "no gateway runner"))

    def start_with_waiter(self, ctx, tmp, **kwargs):
        """Start with a loop that is not up; return the pending marker the waiter thread works for."""
        self.assertFalse(send.start_timer(ctx, wait_poll=0.005, **kwargs))
        self.assertEqual(len(send._timers), 1)
        return next(iter(send._timers.values()))

    def test_a_waiter_hands_the_timer_over_when_the_loop_comes_up(self):
        ctx, loop = self.Ctx(), FakeLoop(running=False)
        with tempfile.TemporaryDirectory() as tmp, self.gateway(loop=loop), self.data_dir(tmp), self.fake_hop():
            with self.assertLogs("snooze.send", "INFO") as logs:
                pending = self.start_with_waiter(ctx, tmp, wait_timeout=10)
                self.assertFalse(pending.done())
                self.assertEqual(self.hops, [])
                loop.running = True
                join_waiters()
            self.assertEqual([hop_loop for _, hop_loop in self.hops], [loop])
            self.assertIs(send._timers[str(tmp)], self.tasks[0])
            self.assertEqual(ctx.spawned, [])
        self.assertTrue(any("started on the gateway loop (waited " in line for line in logs.output))

    def test_a_second_start_while_the_waiter_pends_starts_nothing(self):
        ctx, loop = self.Ctx(), FakeLoop(running=False)
        with tempfile.TemporaryDirectory() as tmp, self.gateway(loop=loop), self.data_dir(tmp), self.fake_hop():
            with self.assertLogs("snooze.send", "INFO"):
                pending = self.start_with_waiter(ctx, tmp, wait_timeout=10)
                self.assertFalse(send.start_timer(ctx, wait_poll=0.005))  # same thread, no loop here
                self.assertIs(send._timers[str(tmp)], pending)
                self.assertEqual([t.name for t in threading.enumerate()].count("snooze-timer-wait"), 1)
                loop.running = True
                join_waiters()
                self.assertFalse(send.start_timer(ctx))  # the hand-over is a live timer now
            self.assertEqual(len(self.hops), 1)

    def test_a_waiter_that_gives_up_closes_the_coroutine_and_a_later_call_retries(self):
        ctx, loop = self.Ctx(), FakeLoop(running=False)
        seen = []
        original = send.Timer.run
        with (
            tempfile.TemporaryDirectory() as tmp,
            self.gateway(loop=loop),
            self.data_dir(tmp),
            self.fake_hop(),
            mock.patch.object(send.Timer, "run", lambda timer: seen.append(original(timer)) or seen[-1]),
            self.assertLogs("snooze.send", "INFO"),
        ):
            pending = self.start_with_waiter(ctx, tmp, wait_timeout=0.03)
            join_waiters()
            self.assertTrue(pending.done())
            self.assertEqual(inspect.getcoroutinestate(seen[0]), inspect.CORO_CLOSED)
            self.assertEqual(self.hops, [])
            loop.running = True
            self.assertTrue(send.start_timer(ctx))  # on a ready loop: starts at once
            self.assertEqual(len(self.hops), 1)

    def test_a_failed_hand_over_ends_the_waiter_and_allows_a_retry(self):
        ctx, loop = self.Ctx(), FakeLoop(running=False)
        seen = []
        original = send.Timer.run
        self.hop_error = RuntimeError("Event loop is closed")
        with (
            tempfile.TemporaryDirectory() as tmp,
            self.gateway(loop=loop),
            self.data_dir(tmp),
            self.fake_hop(),
            mock.patch.object(send.Timer, "run", lambda timer: seen.append(original(timer)) or seen[-1]),
            self.assertLogs("snooze.send", "INFO") as logs,
        ):
            pending = self.start_with_waiter(ctx, tmp, wait_timeout=10)
            loop.running = True
            join_waiters()
            self.assertTrue(pending.done())
            self.assertEqual(inspect.getcoroutinestate(seen[0]), inspect.CORO_CLOSED)
            self.assertEqual(send._timers, {})
        self.assertTrue(any("scheduling failed" in line for line in logs.output))

    def test_the_fallback_on_the_loop_takes_over_from_the_waiter(self):
        ctx, loop = self.Ctx(), FakeLoop(running=False)
        with tempfile.TemporaryDirectory() as tmp, self.gateway(loop=loop), self.data_dir(tmp), self.fake_hop():
            with self.assertLogs("snooze.send", "INFO"):
                pending = self.start_with_waiter(ctx, tmp, wait_timeout=10)
                self.assertTrue(in_loop(lambda: send.start_timer(ctx)))  # the hook, on a loop
                self.assertEqual(len(ctx.spawned), 1)
                self.assertIs(send._timers[str(tmp)], ctx.tasks[0])
                loop.running = True  # the gateway loop comes up after all
                join_waiters()
            self.assertTrue(pending.done())
            self.assertEqual(self.hops, [])  # the waiter stood down: still one timer
            self.assertIs(send._timers[str(tmp)], ctx.tasks[0])
            self.assertEqual(len(ctx.spawned), 1)

    def test_the_fallback_leaves_it_to_a_waiter_that_already_claimed_the_job(self):
        ctx = self.Ctx()
        with tempfile.TemporaryDirectory() as tmp, self.gateway(loop=FakeLoop()), self.data_dir(tmp):
            pending = send._PendingTimer()
            send._timers[str(tmp)] = pending
            self.assertTrue(pending.claim())
            self.assertFalse(pending.done())
            self.assertFalse(in_loop(lambda: send.start_timer(ctx)))
            self.assertEqual(ctx.spawned, [])
            pending.finish()
            self.assertTrue(pending.done())
            self.assertFalse(pending.supersede())

    def test_the_waiter_keeps_the_callers_context(self):
        scope = contextvars.ContextVar("scope", default="unset")
        seen = {}
        ran = threading.Event()

        async def fake_run(timer, interval=0):
            seen["scope"] = scope.get()
            seen["thread"] = threading.current_thread()
            ran.set()

        loop = asyncio.new_event_loop()
        thread = threading.Thread(target=loop.run_forever, daemon=True)
        thread.start()
        self.addCleanup(loop.close)
        self.addCleanup(thread.join, 5)
        self.addCleanup(loop.call_soon_threadsafe, loop.stop)
        started = threading.Event()
        loop.call_soon_threadsafe(started.set)
        self.assertTrue(started.wait(5))
        ctx = self.Ctx()

        def register_like_hermes():
            scope.set("profile-a")
            with tempfile.TemporaryDirectory() as tmp, self.gateway(loop=None), self.data_dir(tmp):
                with mock.patch.object(send.Timer, "run", fake_run), self.assertLogs("snooze.send", "INFO"):
                    self.result = send.start_timer(ctx, wait_poll=0.005, wait_timeout=10)
                    self.runner._gateway_loop = loop  # the loop comes up while the waiter polls
                    self.assertTrue(ran.wait(5))
                    join_waiters()

        worker = threading.Thread(target=register_like_hermes)
        worker.start()
        worker.join(15)
        self.assertFalse(self.result)
        self.assertEqual(seen["scope"], "profile-a")
        self.assertIs(seen["thread"], thread)

    def test_a_failed_hop_closes_the_coroutine(self):
        self.hop_error = RuntimeError("Event loop is closed")
        with tempfile.TemporaryDirectory() as tmp, self.gateway(loop=FakeLoop()), self.data_dir(tmp):
            with self.fake_hop(), self.assertLogs("snooze.send", "WARNING") as logs:
                self.assertFalse(send.start_timer(self.Ctx()))
        self.assertIn("scheduling failed", logs.output[0])
        self.assertEqual(inspect.getcoroutinestate(self.hops[0][0]), inspect.CORO_CLOSED)
        self.assertEqual(send._timers, {})

    def test_a_failed_spawn_closes_the_coroutine(self):
        ctx = self.Ctx(error=RuntimeError("boom"))
        seen = []
        original = send.Timer.run
        with (
            tempfile.TemporaryDirectory() as tmp,
            self.gateway(),
            self.data_dir(tmp),
            mock.patch.object(send.Timer, "run", lambda timer: seen.append(original(timer)) or seen[-1]),
            self.assertLogs("snooze.send", "WARNING"),
        ):
            self.assertFalse(in_loop(lambda: send.start_timer(ctx)))
        self.assertEqual(inspect.getcoroutinestate(seen[0]), inspect.CORO_CLOSED)

    def test_every_outcome_is_logged(self):
        with self.assertLogs("snooze.send", "INFO") as logs, mock.patch.dict(sys.modules, {"gateway.run": None}):
            send.start_timer(self.Ctx())
        self.assertIn("not in the gateway", logs.output[0])
        with self.assertLogs("snooze.send", "INFO") as logs, tempfile.TemporaryDirectory() as tmp:
            with self.gateway(), self.data_dir(tmp):
                send.start_timer(self.Ctx(), wait_timeout=0.01, wait_poll=0.005)
                join_waiters()
        self.assertIn("gateway loop not ready", logs.output[0])
        self.assertTrue(any("gave up waiting" in line for line in logs.output))
        with self.assertLogs("snooze.send", "INFO") as logs, tempfile.TemporaryDirectory() as tmp:
            with self.gateway(), self.data_dir(tmp):
                in_loop(lambda: send.start_timer(self.Ctx()))
        self.assertEqual(logs.output[0].split(":", 2)[2], "snooze timer: started")

    def test_the_timer_really_runs_on_the_gateway_loop_with_the_callers_context(self):
        scope = contextvars.ContextVar("scope", default="unset")
        seen = {}
        ran = threading.Event()

        async def fake_run(timer, interval=0):
            seen["loop"] = asyncio.get_running_loop()
            seen["thread"] = threading.current_thread()
            seen["scope"] = scope.get()
            ran.set()

        loop = asyncio.new_event_loop()
        thread = threading.Thread(target=loop.run_forever, daemon=True)
        thread.start()
        self.addCleanup(loop.close)  # cleanups run last-in first: stop, join, then close
        self.addCleanup(thread.join, 5)
        self.addCleanup(loop.call_soon_threadsafe, loop.stop)
        started = threading.Event()
        loop.call_soon_threadsafe(started.set)  # the loop is running once this fires
        self.assertTrue(started.wait(5))
        ctx = self.Ctx()

        def register_like_hermes():
            # A loader thread: no loop of its own, a profile scope set in its context.
            scope.set("profile-a")
            with tempfile.TemporaryDirectory() as tmp, self.gateway(loop=loop), self.data_dir(tmp):
                with mock.patch.object(send.Timer, "run", fake_run):
                    self.result = send.start_timer(ctx)
                    self.assertTrue(ran.wait(5))

        worker = threading.Thread(target=register_like_hermes)
        worker.start()
        worker.join(10)
        self.assertTrue(self.result)
        self.assertIs(seen["loop"], loop)
        self.assertIs(seen["thread"], thread)
        self.assertEqual(seen["scope"], "profile-a")
        self.assertEqual(ctx.spawned, [])


class RunTest(TimerCase):
    def test_loop_sends_and_frees_the_lock_when_cancelled(self):
        self.add()

        async def scenario():
            task = asyncio.create_task(self.timer.run(interval=0.01))
            for _ in range(200):
                await asyncio.sleep(0.01)
                if self.sender.sent:
                    break
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task

        asyncio.run(scenario())
        self.assertEqual(len(self.sender.sent), 1)
        with SenderLock(self.dir) as held:
            self.assertTrue(held)

    def test_loop_survives_a_corrupt_store(self):
        self.store.path.write_text("{not json")

        async def scenario():
            task = asyncio.create_task(self.timer.run(interval=0.01))
            await asyncio.sleep(0.05)
            self.assertFalse(task.done())
            task.cancel()

        with self.assertLogs("snooze.send", "WARNING"):
            asyncio.run(scenario())


if __name__ == "__main__":
    unittest.main()
