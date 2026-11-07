"""Snooze store: one JSON file per profile, guarded by a file lock.

Pure module: standard library only, no Hermes import. The caller passes the
data directory.

Files in the data directory:
    snoozes.json   the records (written atomically: temp file + rename)
    snoozes.lock   exclusive lock held during every read-modify-write
    sender.lock    held by the one process that sends reminders

Timestamps are ISO 8601 strings in UTC (for example "2026-11-07T18:00:00+00:00").
The store does not interpret them; `schedule.py` does. The fields `every`,
`confirm` and `then` are plain JSON values owned by the modules that use them.
"""
from __future__ import annotations

import contextlib
import dataclasses
import fcntl
import json
import os
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterator, TypeVar

T = TypeVar("T")

FILE_NAME = "snoozes.json"
LOCK_NAME = "snoozes.lock"
SENDER_LOCK_NAME = "sender.lock"
FORMAT_VERSION = 1

PENDING, ACTIVE, DONE, MISSED, CANCELLED = "pending", "active", "done", "missed", "cancelled"
STATES = (PENDING, ACTIVE, DONE, MISSED, CANCELLED)

DEFAULT_LOCK_TIMEOUT = 10.0


class StoreError(Exception):
    """The store file is unreadable or invalid, or a lock could not be taken."""


@dataclass
class Target:
    """Where the reminder goes."""

    platform: str
    chat_id: Any
    user_id: Any = None


@dataclass
class Schedule:
    """When the reminder fires. All times are UTC ISO strings; `tz` is the user's zone name."""

    first_at: str
    every: Any = None
    until: str | None = None
    tz: str = "UTC"


@dataclass(kw_only=True)
class Snooze:
    """One reminder record. `id` is 0 until the store assigns it in `Txn.add`."""

    id: int = 0
    text: str
    created: str
    target: Target
    schedule: Schedule
    confirm: Any = None
    then: Any = None
    state: str = PENDING
    sent_count: int = 0
    last_sent_at: str | None = None
    next_at: str | None = None
    last_message_id: Any = None
    last_error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)

    @classmethod
    def from_dict(cls, data: Any) -> "Snooze":
        """Build a record from parsed JSON. Raises StoreError if it is malformed."""
        try:
            if not isinstance(data, dict):
                raise ValueError("record is not an object")
            target = data["target"]
            schedule = data["schedule"]
            if not isinstance(target, dict) or not isinstance(schedule, dict):
                raise ValueError("target and schedule must be objects")
            rec = cls(
                id=_int(data["id"], "id"),
                text=_str(data["text"], "text"),
                created=_str(data["created"], "created"),
                target=Target(**_known(Target, target)),
                schedule=Schedule(**_known(Schedule, schedule)),
                **_known(cls, data, skip=("id", "text", "created", "target", "schedule")),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise StoreError(f"invalid snooze record ({type(exc).__name__}: {exc})") from exc
        if rec.state not in STATES:
            raise StoreError(f"invalid snooze record: unknown state {rec.state!r}")
        if not isinstance(rec.sent_count, int) or isinstance(rec.sent_count, bool):
            raise StoreError("invalid snooze record: sent_count must be an integer")
        return rec


def _known(klass: type, data: dict, skip: tuple[str, ...] = ()) -> dict[str, Any]:
    """Keep only the keys that are fields of `klass` (unknown keys are ignored)."""
    names = {f.name for f in dataclasses.fields(klass)} - set(skip)
    return {k: v for k, v in data.items() if k in names}


def _int(value: Any, name: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError(f"{name} must be an integer")
    return value


def _str(value: Any, name: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{name} must be a string")
    return value


@dataclass
class Txn:
    """The records inside one `Store.transaction()`. Mutate `snoozes` freely; it is saved on clean exit."""

    snoozes: list[Snooze]
    next_id: int

    def add(self, snooze: Snooze) -> Snooze:
        """Give `snooze` the next free id and append it. Ids are never reused."""
        used = max((s.id for s in self.snoozes), default=0)
        snooze.id = max(self.next_id, used + 1)
        self.next_id = snooze.id + 1
        self.snoozes.append(snooze)
        return snooze

    def get(self, snooze_id: int) -> Snooze | None:
        for s in self.snoozes:
            if s.id == snooze_id:
                return s
        return None


class Store:
    """Snoozes of one profile, kept in `<data_dir>/snoozes.json`."""

    def __init__(self, data_dir: str | os.PathLike, lock_timeout: float = DEFAULT_LOCK_TIMEOUT):
        self.data_dir = Path(data_dir)
        self.path = self.data_dir / FILE_NAME
        self.lock_path = self.data_dir / LOCK_NAME
        self.lock_timeout = lock_timeout

    # -- reading -----------------------------------------------------------

    def load(self) -> list[Snooze]:
        """Return all records. A missing file is an empty store; a corrupt one raises StoreError."""
        return self._read()[0]

    def get(self, snooze_id: int) -> Snooze | None:
        for s in self.load():
            if s.id == snooze_id:
                return s
        return None

    # -- writing -----------------------------------------------------------

    @contextlib.contextmanager
    def transaction(self) -> Iterator[Txn]:
        """Read-modify-write under the exclusive lock. Saves on clean exit, discards on exception."""
        with _flock(self.lock_path, blocking=True, timeout=self.lock_timeout) as held:
            if not held:
                raise StoreError(f"timed out after {self.lock_timeout}s waiting for lock {self.lock_path}")
            snoozes, next_id = self._read()
            txn = Txn(snoozes=snoozes, next_id=next_id)
            yield txn
            self._write(txn.snoozes, txn.next_id)

    def update(self, fn: Callable[[Txn], T]) -> T:
        """Run `fn(txn)` inside a transaction and return its result."""
        with self.transaction() as txn:
            return fn(txn)

    def add(self, snooze: Snooze) -> Snooze:
        """Store a new record and return it with its assigned id."""
        with self.transaction() as txn:
            return txn.add(snooze)

    def save(self, snoozes: list[Snooze]) -> None:
        """Replace all records. Prefer `transaction()` for read-modify-write."""
        with self.transaction() as txn:
            txn.snoozes = list(snoozes)

    def sender_lock(self) -> "SenderLock":
        return sender_lock(self.data_dir)

    # -- file access -------------------------------------------------------

    def _read(self) -> tuple[list[Snooze], int]:
        try:
            raw = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return [], 1
        except (OSError, UnicodeDecodeError) as exc:
            raise StoreError(f"cannot read {self.path}: {exc}") from exc
        try:
            doc = json.loads(raw)
        except ValueError as exc:
            raise StoreError(f"{self.path} is corrupt (not valid JSON: {exc}); fix or delete it") from exc
        try:
            if not isinstance(doc, dict) or not isinstance(doc.get("snoozes"), list):
                raise StoreError("expected an object with a 'snoozes' list")
            snoozes = [Snooze.from_dict(item) for item in doc["snoozes"]]
            next_id = _int(doc.get("next_id", 1), "next_id")
        except (StoreError, ValueError) as exc:
            raise StoreError(f"{self.path} is corrupt ({exc}); fix or delete it") from exc
        ids = [s.id for s in snoozes]
        if len(ids) != len(set(ids)):
            raise StoreError(f"{self.path} is corrupt (duplicate ids); fix or delete it")
        return snoozes, next_id

    def _write(self, snoozes: list[Snooze], next_id: int) -> None:
        doc = {"version": FORMAT_VERSION, "next_id": next_id, "snoozes": [s.to_dict() for s in snoozes]}
        try:
            data = json.dumps(doc, indent=2, ensure_ascii=False)
        except (TypeError, ValueError) as exc:
            raise StoreError(f"cannot serialize snoozes: {exc}") from exc
        tmp_name = None
        try:
            self.data_dir.mkdir(parents=True, exist_ok=True)
            fd, tmp_name = tempfile.mkstemp(dir=self.data_dir, prefix=FILE_NAME + ".", suffix=".tmp")
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(data)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp_name, self.path)
            tmp_name = None
        except OSError as exc:
            raise StoreError(f"cannot write {self.path}: {exc}") from exc
        finally:
            if tmp_name is not None:
                with contextlib.suppress(OSError):
                    os.unlink(tmp_name)


# -- locks -----------------------------------------------------------------


@contextlib.contextmanager
def _flock(path: Path, blocking: bool, timeout: float = 0.0) -> Iterator[bool]:
    """Hold an exclusive flock on `path`. Yields True if taken, False if not (only when not blocking forever)."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    except OSError as exc:
        raise StoreError(f"cannot open lock file {path}: {exc}") from exc
    held = False
    try:
        deadline = time.monotonic() + timeout
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                held = True
                break
            except BlockingIOError:
                if not blocking or time.monotonic() >= deadline:
                    break
                time.sleep(0.01)
            except OSError as exc:
                raise StoreError(f"cannot lock {path}: {exc}") from exc
        yield held
    finally:
        # Closing the descriptor drops the lock (also if the process dies).
        os.close(fd)


class SenderLock:
    """Non-blocking lock for the single process that sends reminders.

    Use as `with sender_lock(dir) as held:` (held is a bool), or call
    `acquire()` / `release()` to keep it for the life of a timer loop.
    The OS frees it if the process dies.
    """

    def __init__(self, path: Path):
        self.path = Path(path)
        self._fd: int | None = None

    @property
    def held(self) -> bool:
        return self._fd is not None

    def acquire(self) -> bool:
        """Try to take the lock without waiting. True if this object now holds it."""
        if self._fd is not None:
            return True
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
        except OSError as exc:
            raise StoreError(f"cannot open lock file {self.path}: {exc}") from exc
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(fd)
            return False
        except OSError as exc:
            os.close(fd)
            raise StoreError(f"cannot lock {self.path}: {exc}") from exc
        self._fd = fd
        return True

    def release(self) -> None:
        if self._fd is not None:
            os.close(self._fd)
            self._fd = None

    def __enter__(self) -> bool:
        return self.acquire()

    def __exit__(self, *exc: object) -> None:
        self.release()


def sender_lock(data_dir: str | os.PathLike) -> SenderLock:
    """Lock object for `<data_dir>/sender.lock`; only one process can hold it at a time."""
    return SenderLock(Path(data_dir) / SENDER_LOCK_NAME)
