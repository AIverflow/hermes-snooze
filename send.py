"""Reminder timer: sends the reminders that are due, with no LLM.

Hermes glue. `Timer.tick()` is one pass over the store; `Timer.run()` repeats it every
`TICK_SECONDS` in a background task started by `register()`. The rules (what is due, what
comes next) are in `schedule.py`; the texts are in `echo.py`. Sending goes through a `Sender`,
so tests use a fake one. See `docs/user-guide.md` for the reminder lifecycle.

Hermes imports stay inside functions: the package loads without Hermes.
"""

from __future__ import annotations

import asyncio
import contextvars
import json
import logging
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Protocol

from . import echo
from .commands import hermes_data_dir, not_started_message, read_settings
from .schedule import EXPIRE, MISS, NOTHING, SEND, Action, after_send, due, is_recurring, rearm
from .store import ACTIVE, MISSED, SenderLock, Snooze, Store, StoreError, Target

logger = logging.getLogger(__name__)

TICK_SECONDS = 15
MAX_ERROR_CHARS = 300


class SendError(Exception):
    """A reminder could not be delivered. The text is kept in `Snooze.last_error`."""


class Sender(Protocol):
    """Delivers one text to one chat. Blocking: the timer calls it from a worker thread."""

    def send(self, target: Target, text: str) -> str | None:
        """Send `text` to `target`. Return the sent message id, or None if the platform gives none.

        Raises `SendError` on failure.
        """
        ...


# -- Hermes access (imports only inside the helpers) ---------------------------


class HermesSender:
    """Sends through Hermes's own `send_message` path (the one behind `hermes send`): no LLM.

    It takes a `platform:chat_id` target, so it works for every platform Hermes can send to. In the
    gateway it uses the live adapter; elsewhere the platform's standalone sender.
    """

    def send(self, target: Target, text: str) -> str | None:
        from tools.send_message_tool import send_message_tool

        args = {"action": "send", "target": f"{target.platform}:{target.chat_id}", "message": text}
        try:
            result = json.loads(send_message_tool(args))
        except Exception as exc:  # a platform SDK error, or a reply that is not JSON
            raise SendError(f"{type(exc).__name__}: {exc}") from exc
        if not isinstance(result, dict) or not result.get("success"):
            error = result.get("error") if isinstance(result, dict) else None
            raise SendError(str(error or "send failed"))
        message_id = result.get("message_id")
        return None if message_id in (None, "") else str(message_id)


def hermes_is_paused() -> bool:
    """True while `hermes pause` is engaged (the ESTOP sentinel exists). False if Hermes has no such switch."""
    try:
        from agent.estop import is_engaged
    except ImportError:
        return False
    return bool(is_engaged())


def in_gateway() -> bool:
    """True in a running gateway process: the one that has the live adapters and receives the replies."""
    module = sys.modules.get("gateway.run")  # not imported outside the gateway; do not import it here
    ref = getattr(module, "_gateway_runner_ref", None)
    return ref is not None and ref() is not None


# -- the timer -----------------------------------------------------------------


@dataclass
class _Job:
    """One message to send, picked inside a transaction and sent outside it."""

    snooze_id: int
    action: Action
    target: Target
    text: str
    next_at: str | None  # the snooze's `next_at` when picked, to notice a `later` made during the send
    first_at: str  # the snooze's `schedule.first_at` when picked: it changes when a recurring snooze is re-armed


@dataclass
class _Result:
    job: _Job
    message_id: str | None
    error: str | None


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _error_text(exc: BaseException) -> str:
    text = str(exc) or type(exc).__name__
    return text if len(text) <= MAX_ERROR_CHARS else text[: MAX_ERROR_CHARS - 1] + "…"


class Timer:
    """Sends due reminders for one profile's store. Only the holder of the sender lock sends."""

    def __init__(
        self,
        store: Store,
        sender: Sender,
        lock: SenderLock,
        *,
        cap_reminders: Callable[[], int],
        now: Callable[[], datetime] = _utcnow,
        is_paused: Callable[[], bool] = lambda: False,
    ):
        self.store = store
        self.sender = sender
        self.lock = lock
        self._cap_reminders = cap_reminders
        self._now = now
        self._is_paused = is_paused

    def tick(self) -> int:
        """One pass: send what is due. Returns the number of messages sent.

        Does nothing if another process holds the sender lock or Hermes is paused. After a pause
        (or a sleep) a due snooze gets one late reminder, not a burst: `due` and `after_send` see to it.
        """
        if not self.lock.acquire() or self._is_paused():
            return 0
        now = self._now()
        cap = self._cap_reminders()
        # Cheap look first (the store is replaced atomically): most ticks have nothing to do and write nothing.
        if all(due(s, now, cap).kind == NOTHING for s in self.store.load()):
            return 0
        jobs = self.store.update(lambda txn: self._pick(txn.snoozes, now, cap))
        results = [self._send(job) for job in jobs]  # no store lock held while sending
        if results:
            self.store.update(lambda txn: self._save(txn.snoozes, results))
        return sum(1 for r in results if r.error is None)

    def _pick(self, snoozes: list[Snooze], now: datetime, cap: int) -> list[_Job]:
        """Under the store lock: decide each snooze's action. A miss or expiry is recorded now, so it happens once."""
        jobs = []
        for snooze in snoozes:
            action = due(snooze, now, cap)
            if action.kind == SEND:
                text = echo.reminder_text(snooze, action.late_by)
            elif action.kind == MISS:
                text = echo.missed_text(snooze)
                if is_recurring(snooze):
                    next_at = rearm(snooze, now)
                    text += f"\nNext: {echo.format_when(next_at, snooze.schedule.tz, now)}"
                else:
                    snooze.state = MISSED
            elif action.kind == EXPIRE:
                text = not_started_message(snooze)
                snooze.state = MISSED
            else:
                continue
            jobs.append(_Job(snooze.id, action, snooze.target, text, snooze.next_at, snooze.schedule.first_at))
        return jobs

    def _send(self, job: _Job) -> _Result:
        try:
            result = _Result(job, self.sender.send(job.target, job.text), None)
            logger.info("snooze #%s: %s sent", job.snooze_id, job.action.kind)
            return result
        except Exception as exc:  # any failure is a retry (or, for a miss, a `last_error`), never a crash
            logger.warning("snooze #%s: send failed: %s", job.snooze_id, exc)
            return _Result(job, None, _error_text(exc))

    def _save(self, snoozes: list[Snooze], results: list[_Result]) -> None:
        """Under the store lock: record what was sent. Each snooze is read again: it may have changed meanwhile."""
        by_id = {s.id: s for s in snoozes}
        sent_at = self._now()
        for result in results:
            snooze = by_id.get(result.job.snooze_id)
            if snooze is None:
                continue
            if result.job.action.kind == SEND and snooze.schedule.first_at != result.job.first_at:
                continue  # answered and re-armed while sending: this message or error belongs to a finished occurrence
            if result.error is not None:
                snooze.last_error = result.error  # a failed reminder keeps its due time: the next tick retries it
                continue
            if result.job.action.kind == SEND and result.message_id is not None:
                snooze.last_message_id = result.message_id
            if result.job.action.kind != SEND:
                snooze.last_error = None
            elif snooze.state != ACTIVE or snooze.next_at != result.job.next_at:
                # Confirmed, cancelled or pushed back (`later`) while sending: count it, keep their change.
                snooze.sent_count += 1
                snooze.last_sent_at = sent_at.astimezone(timezone.utc).isoformat()
            else:
                after_send(snooze, sent_at)

    async def run(self, interval: float = TICK_SECONDS) -> None:
        """Tick forever. Retries the sender lock each time, so another process takes over if the holder dies."""
        logger.info("snooze timer: loop started (every %ss)", interval)
        try:
            while True:
                try:
                    await asyncio.to_thread(self.tick)
                except StoreError as exc:
                    logger.warning("snooze timer: %s", exc)
                except Exception:
                    logger.exception("snooze timer: tick failed")
                await asyncio.sleep(interval)
        finally:
            self.lock.release()
            logger.info("snooze timer: loop stopped")


# One timer per data directory (= per profile) in this process: `register()` and the first gateway
# hook call may both ask for it. Keys are `str(data_dir)`. A value is a task or future, or a
# `_PendingTimer` while a waiter thread is still looking for the gateway loop.
_timers: dict[str, Any] = {}

WAIT_TIMEOUT_SECONDS = 120
WAIT_POLL_SECONDS = 1.0


def _gateway_loop_state() -> tuple[asyncio.AbstractEventLoop | None, str]:
    """The gateway's own event loop if it is up, else (None, why not)."""
    module = sys.modules.get("gateway.run")
    ref = getattr(module, "_gateway_runner_ref", None)
    runner = ref() if ref is not None else None
    if runner is None:
        return None, "no gateway runner"
    loop = getattr(runner, "_gateway_loop", None)
    if loop is None:
        return None, "_gateway_loop is None"
    if loop.is_closed():
        return None, "loop closed"
    if not loop.is_running():
        return None, "loop not running"
    return loop, ""


def gateway_loop() -> asyncio.AbstractEventLoop | None:
    """The gateway's own event loop if it is up, else None.

    Hermes itself schedules onto it from other threads (gateway/run_plugin_injection.py). It is
    not necessarily up while `register()` runs: see `start_timer`.
    """
    return _gateway_loop_state()[0]


class _PendingTimer:
    """Stands in `_timers` while a waiter thread looks for the gateway loop; it quacks like a task.

    One of three things ends the wait, and the lock makes them exclusive: the waiter hands the timer
    over (`claim`), the first-message fallback starts its own on the loop (`supersede`), or the waiter
    gives up (`finish`). `done()` is False while the waiter may still start the timer.
    """

    _WAITING, _CLAIMED, _SUPERSEDED, _FINISHED = range(4)

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._state = self._WAITING
        self.superseded = threading.Event()  # wakes the waiter's sleep

    def claim(self) -> bool:
        """The waiter is about to schedule the timer. False if the fallback got there first."""
        with self._lock:
            if self._state != self._WAITING:
                return False
            self._state = self._CLAIMED
            return True

    def supersede(self) -> bool:
        """The fallback starts the timer itself. False if the waiter already claimed the job."""
        with self._lock:
            if self._state != self._WAITING:
                return False
            self._state = self._SUPERSEDED
            self.superseded.set()
            return True

    def finish(self) -> None:
        with self._lock:
            if self._state != self._SUPERSEDED:
                self._state = self._FINISHED

    def done(self) -> bool:
        return self._state in (self._SUPERSEDED, self._FINISHED)


def _wait_and_hand_over(
    key: str,
    pending: _PendingTimer,
    coro: Any,
    timeout: float,
    poll: float,
    reason: str,
) -> None:
    """Waiter thread body: poll until the gateway loop is up, then schedule `coro` on it.

    Closes `coro` on every path that does not schedule it, and always ends `pending`, so a later
    `start_timer` can retry after a failure or a give-up.
    """
    handed = False
    try:
        logger.info("snooze timer: waiting for the gateway loop (%s)", reason)
        started = time.monotonic()
        while True:
            if pending.superseded.is_set():
                logger.info("snooze timer: waiter stopped, the timer was started from the loop")
                return
            loop, reason = _gateway_loop_state()
            if loop is not None:
                break
            waited = time.monotonic() - started
            if waited >= timeout:
                logger.info("snooze timer: gave up waiting for the gateway loop after %.0fs (%s)", waited, reason)
                return
            pending.superseded.wait(min(poll, timeout - waited))
        if not pending.claim():
            logger.info("snooze timer: waiter stopped, the timer was started from the loop")
            return
        try:
            future = asyncio.run_coroutine_threadsafe(coro, loop)
        except Exception:  # a loop closed since the check above
            logger.warning("snooze timer: not started (scheduling failed)", exc_info=True)
            return
        handed = True
        if _timers.get(key) is pending:
            _timers[key] = future
        logger.info("snooze timer: started on the gateway loop (waited %.0fs)", time.monotonic() - started)
    except Exception:
        logger.exception("snooze timer: waiter failed")
    finally:
        if not handed:
            coro.close()
            if _timers.get(key) is pending:
                del _timers[key]
        pending.finish()


def start_timer(ctx: Any, *, wait_timeout: float = WAIT_TIMEOUT_SECONDS, wait_poll: float = WAIT_POLL_SECONDS) -> bool:
    """Start the timer loop for the active profile. True if it was started by this call.

    Only a gateway process runs it: it has the live adapters and gets the replies, and a CLI or
    Desktop process that held the lock would keep the gateway from sending. The profile is the
    active one (data directory and credentials the task inherits), so call it inside its scope.
    Idempotent: a profile that already has a live timer task, or a waiter, gets no second one.

    Three routes. With a running loop in this thread, `ctx.spawn_task`: Hermes supervises the task and
    cancels it on unload. Hermes runs `register()` on a plugin-load worker thread that has no loop
    (hermes_cli/plugins_loader.py `run_with_load_deadline`), so there the task is handed to the
    gateway's loop with `run_coroutine_threadsafe`. That call copies this thread's contextvars
    (`call_soon_threadsafe` with no context), so the task keeps the profile scope. The gateway loop
    is not always up yet at that point (live finding: `_gateway_loop` unset or not running), so
    then a short-lived waiter thread polls for it and does the hand-over; this call returns False.
    A `start_timer` call that runs on a loop while a waiter pends (the first-message fallback in
    `hooks.Hooks`) takes over and the waiter stands down.
    """
    if not in_gateway():
        logger.info("snooze timer: not started (not in the gateway)")
        return False
    data_dir = hermes_data_dir()
    key = str(data_dir)
    existing = _timers.get(key)
    try:
        asyncio.get_running_loop()
        on_this_loop = True
    except RuntimeError:
        on_this_loop = False
    if on_this_loop and isinstance(existing, _PendingTimer):
        if not existing.supersede():
            logger.debug("snooze timer: the waiter is already starting it")
            return False
        logger.info("snooze timer: starting from the loop, the waiter stands down")
        existing = None
    if not may_start(existing):
        logger.debug("snooze timer: already running")
        return False
    loop, reason = (None, "") if on_this_loop else _gateway_loop_state()
    timer = Timer(
        Store(data_dir),
        HermesSender(),
        SenderLock(data_dir),
        cap_reminders=lambda: read_settings(ctx).cap_reminders,
        is_paused=hermes_is_paused,
    )
    coro = timer.run()
    if not on_this_loop and loop is None:
        logger.info("snooze timer: gateway loop not ready (%s)", reason)
        return _start_waiter(key, coro, wait_timeout, wait_poll, reason)
    try:
        if loop is None:
            _timers[key] = ctx.spawn_task(coro, name="snooze:timer")
        else:
            _timers[key] = asyncio.run_coroutine_threadsafe(coro, loop)
    except Exception:  # a closed loop, or a Hermes without spawn_task
        coro.close()
        logger.warning("snooze timer: not started (scheduling failed)", exc_info=True)
        return False
    logger.info("snooze timer: started" if loop is None else "snooze timer: started on the gateway loop")
    return True


def _start_waiter(key: str, coro: Any, timeout: float, poll: float, reason: str) -> bool:
    """Record a pending marker and start the waiter thread. Always False: nothing is running yet."""
    pending = _PendingTimer()
    _timers[key] = pending  # before the thread starts: a second call must see it
    # A plain thread does not inherit contextvars, and the profile scope (data dir, credentials) lives there.
    scope = contextvars.copy_context()
    thread = threading.Thread(
        target=scope.run,
        args=(_wait_and_hand_over, key, pending, coro, timeout, poll, reason),
        name="snooze-timer-wait",
        daemon=True,
    )
    try:
        thread.start()
    except Exception:
        coro.close()
        del _timers[key]
        logger.warning("snooze timer: not started (waiter thread failed)", exc_info=True)
    return False


def may_start(task: Any) -> bool:
    """True if there is no live timer task (`task` is the one recorded for the profile, or None)."""
    done = getattr(task, "done", None)
    return task is None or done is None or bool(done())
