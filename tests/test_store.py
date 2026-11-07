import json
import multiprocessing
import tempfile
import unittest
from pathlib import Path

from snooze import store
from snooze.store import Schedule, Snooze, Store, StoreError, Target


def make(text="Send email to John Doe", **kw):
    return Snooze(
        text=text,
        created="2026-11-07T10:00:00+00:00",
        target=Target(platform="telegram", chat_id="123", user_id="42"),
        schedule=Schedule(
            first_at="2026-11-07T17:00:00+00:00", every="30m", until="2026-11-07T18:00:00+00:00", tz="Europe/Paris"
        ),
        confirm={"kind": "word", "word": "ok"},
        then="Draft the reply",
        **kw,
    )


def _add_many(data_dir, tag, count):
    s = Store(data_dir)
    for i in range(count):
        s.add(make(f"{tag}-{i}"))


def _hold_sender(data_dir, ready, release):
    with store.SenderLock(data_dir) as held:
        ready.put(held)
        if held:
            release.get(timeout=20)


def _try_sender(data_dir, out):
    with store.SenderLock(data_dir) as held:
        out.put(held)


class TmpCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name) / "data"  # does not exist yet
        self.store = Store(self.dir)


class RoundTripTest(TmpCase):
    def test_missing_file_is_empty(self):
        self.assertEqual(self.store.load(), [])

    def test_round_trip_all_fields(self):
        s = make(
            state=store.ACTIVE,
            sent_count=3,
            last_sent_at="2026-11-07T17:30:00+00:00",
            next_at="2026-11-07T18:00:00+00:00",
            last_message_id=99,
            last_error="boom",
        )
        saved = self.store.add(s)
        self.assertEqual(saved.id, 1)
        loaded = Store(self.dir).load()
        self.assertEqual(loaded, [s])
        self.assertEqual(loaded[0].schedule.tz, "Europe/Paris")
        self.assertEqual(loaded[0].target.chat_id, "123")

    def test_ids_increment_and_are_not_reused(self):
        a, b = self.store.add(make("a")), self.store.add(make("b"))
        self.assertEqual((a.id, b.id), (1, 2))
        self.store.update(lambda t: t.snoozes.remove(t.get(2)))
        self.assertEqual(self.store.add(make("c")).id, 3)

    def test_update_changes_a_record(self):
        sn = self.store.add(make())

        def done(txn):
            txn.get(sn.id).state = store.DONE
            return "ok"

        self.assertEqual(self.store.update(done), "ok")
        self.assertEqual(self.store.get(sn.id).state, store.DONE)

    def test_exception_in_transaction_discards_changes(self):
        sn = self.store.add(make())
        with self.assertRaises(RuntimeError):
            with self.store.transaction() as txn:
                txn.get(sn.id).state = store.CANCELLED
                raise RuntimeError("nope")
        self.assertEqual(self.store.get(sn.id).state, store.PENDING)

    def test_unknown_keys_ignored(self):
        self.store.add(make())
        doc = json.loads(self.store.path.read_text())
        doc["snoozes"][0]["future_field"] = 1
        self.store.path.write_text(json.dumps(doc))
        self.assertEqual(len(self.store.load()), 1)

    def test_no_temp_files_left(self):
        self.store.add(make())
        names = sorted(p.name for p in self.dir.iterdir())
        self.assertEqual(names, sorted([store.LOCK_NAME, store.FILE_NAME]))


class WritersTest(TmpCase):
    def test_two_processes_lose_nothing(self):
        n = 25
        procs = [multiprocessing.Process(target=_add_many, args=(self.dir, tag, n)) for tag in "AB"]
        for p in procs:
            p.start()
        for p in procs:
            p.join(60)
            self.assertEqual(p.exitcode, 0)
        loaded = self.store.load()
        self.assertEqual(len(loaded), 2 * n)
        self.assertEqual(len({s.id for s in loaded}), 2 * n)
        self.assertEqual({s.text for s in loaded}, {f"{t}-{i}" for t in "AB" for i in range(n)})

    def test_lock_timeout_is_a_store_error(self):
        slow = Store(self.dir, lock_timeout=0.2)
        with self.store.transaction():
            with self.assertRaises(StoreError):
                slow.add(make())


class CorruptTest(TmpCase):
    def put(self, text):
        self.dir.mkdir(parents=True, exist_ok=True)
        self.store.path.write_text(text)

    def test_bad_json(self):
        self.put("{not json")
        with self.assertRaises(StoreError) as cm:
            self.store.load()
        self.assertIn("corrupt", str(cm.exception))
        self.assertIn(str(self.store.path), str(cm.exception))

    def test_empty_file(self):
        self.put("")
        with self.assertRaises(StoreError):
            self.store.load()

    def test_wrong_shape(self):
        for text in ("[]", '{"snoozes": 3}', '{"snoozes": [1]}', '{"snoozes": [{"id": 1}]}'):
            self.put(text)
            with self.assertRaises(StoreError, msg=text):
                self.store.load()

    def test_bad_state(self):
        self.store.add(make())
        doc = json.loads(self.store.path.read_text())
        doc["snoozes"][0]["state"] = "weird"
        self.store.path.write_text(json.dumps(doc))
        with self.assertRaises(StoreError):
            self.store.load()

    def test_write_does_not_overwrite_corrupt_file(self):
        self.put("{not json")
        with self.assertRaises(StoreError):
            self.store.add(make())
        self.assertEqual(self.store.path.read_text(), "{not json")


class SenderLockTest(TmpCase):
    def test_only_one_process_holds_it(self):
        ctx = multiprocessing.get_context()
        ready, release = ctx.Queue(), ctx.Queue()
        holder = ctx.Process(target=_hold_sender, args=(self.dir, ready, release))
        holder.start()
        try:
            self.assertTrue(ready.get(timeout=20))
            out = ctx.Queue()
            other = ctx.Process(target=_try_sender, args=(self.dir, out))
            other.start()
            other.join(20)
            self.assertFalse(out.get(timeout=5))
            lock = store.SenderLock(self.dir)
            self.assertFalse(lock.acquire())
        finally:
            release.put(True)
            holder.join(20)
        # Holder gone: the lock is free again.
        lock = store.SenderLock(self.dir)
        self.assertTrue(lock.acquire())
        lock.release()

    def test_released_after_with(self):
        with store.SenderLock(self.dir) as held:
            self.assertTrue(held)
            with store.SenderLock(self.dir) as second:
                self.assertFalse(second)
        with store.SenderLock(self.dir) as again:
            self.assertTrue(again)

    def test_sender_lock_does_not_block_store(self):
        with store.SenderLock(self.dir):
            self.store.add(make())
        self.assertEqual(len(self.store.load()), 1)


if __name__ == "__main__":
    unittest.main()
