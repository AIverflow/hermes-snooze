import argparse
import io
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from snooze import cli, commands, send
from snooze.commands import Commands
from snooze.store import ACTIVE, CANCELLED, DONE, MISSED, Store, Target

NOW = datetime(2026, 11, 7, 10, 0, tzinfo=timezone.utc)
HOME = Target(platform="telegram", chat_id="999", user_id="999")


class FakeCtx:
    def __init__(self, **config):
        self.config = {"timezone": "Europe/Paris", **config}
        self.commands = {}
        self.tools = {}
        self.cli_commands = {}

    def get_config(self, key, default=None):
        return self.config.get(key, default)

    def register_command(self, name, handler, **kwargs):
        self.commands[name] = handler

    def register_tool(self, name, toolset, schema, handler, **kwargs):
        self.tools[name] = handler

    def register_cli_command(self, name, **kwargs):
        self.cli_commands[name] = kwargs


class CliTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.data_dir = Path(self.tmp.name) / "data"
        self.ctx = FakeCtx()
        self.commands = Commands(
            self.ctx,
            data_dir=lambda: self.data_dir,
            now=lambda: NOW,
            home_target=lambda platform=None: HOME,
            session_env=lambda name: "",
            gateway_running=lambda: False,
        )
        self.parser = argparse.ArgumentParser()
        cli._setup(self.parser)

    def run_cli(self, *argv):
        args = self.parser.parse_args(argv)
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = cli._handler(self.commands, args)
        return code, stdout.getvalue(), stderr.getvalue()

    def test_add_prints_id_and_summary_and_stores_home_target(self):
        code, stdout, stderr = self.run_cli("add", "Backup failed in 1m ok retry|skip")

        self.assertEqual(code, 0)
        self.assertEqual(stdout, "1\n")
        self.assertIn("Snooze #1", stderr)
        self.assertIn("gateway", stderr.lower())
        snooze = Store(self.data_dir).get(1)
        self.assertEqual(snooze.target, HOME)
        self.assertEqual(snooze.state, ACTIVE)

    def _run_waiting_add(self, text, states, timeout=0):
        elapsed = [0]
        wait_impl = cli._wait

        def sleep(_seconds):
            elapsed[0] += 2
            state, answer = states.pop(0)

            def update(txn):
                record = txn.get(max(item.id for item in txn.snoozes))
                record.state = state
                record.answer = answer

            Store(self.data_dir).update(update)

        def wait(store, snooze_id, wait_timeout):
            return wait_impl(store, snooze_id, wait_timeout, sleep_fn=sleep, clock=lambda: elapsed[0])

        with mock.patch("snooze.cli._wait", side_effect=wait):
            return self.run_cli("add", text, "--wait", "--timeout", str(timeout))

    def test_wait_done_prints_answer(self):
        self.assertEqual(
            self._run_waiting_add("Dinner in 1m ok asian|french", [(DONE, "B")]),
            (0, "B\n", mock.ANY),
        )

    def test_wait_done_without_answer_prints_empty_line(self):
        code, stdout, _stderr = self._run_waiting_add("Dinner in 1m", [(DONE, None)])
        self.assertEqual((code, stdout), (0, "\n"))

    def test_wait_missed_and_cancelled_exit_codes(self):
        for state, expected in ((MISSED, 3), (CANCELLED, 4)):
            with self.subTest(state=state):
                code, stdout, _stderr = self._run_waiting_add("Dinner in 1m", [(state, None)])
                self.assertEqual((code, stdout), (expected, ""))

    def test_wait_timeout_keeps_active_record(self):
        code, stdout, stderr = self._run_waiting_add("Dinner in 1m", [(ACTIVE, None)], timeout=1)
        self.assertEqual((code, stdout), (5, ""))
        self.assertIn("Still running: #1", stderr)
        self.assertEqual(Store(self.data_dir).get(1).state, ACTIVE)

    def test_wait_refuses_recurring_without_creating(self):
        code, stdout, stderr = self.run_cli("add", "Pill at 8am daily", "--wait")
        self.assertEqual((code, stdout), (2, ""))
        self.assertIn("--wait does not work with a recurring snooze", stderr)
        self.assertEqual(Store(self.data_dir).load(), [])

    def test_wait_pending_then_active_then_done(self):
        self.ctx.config["validate_setup"] = True
        code, stdout, _stderr = self._run_waiting_add("Dinner in 1m", [(ACTIVE, None), (ACTIVE, None), (DONE, "ready")])
        self.assertEqual((code, stdout), (0, "ready\n"))

    def test_parse_error_exits_two_without_stdout(self):
        code, stdout, stderr = self.run_cli("add", "Backup failed")

        self.assertEqual(code, 2)
        self.assertEqual(stdout, "")
        self.assertIn("Missing when to start", stderr)

    def test_no_target_exits_two(self):
        self.commands._home_target = lambda platform=None: None

        code, stdout, stderr = self.run_cli("add", "Backup failed in 1m")

        self.assertEqual(code, 2)
        self.assertEqual(stdout, "")
        self.assertIn("No chat to send reminders to", stderr)

    def test_status_prints_state_and_answer_when_present(self):
        record, _message = self.commands.create_from_cli("Dinner in 1m ok asian|french")
        self.assertIsNotNone(record)
        self.assertEqual(self.run_cli("status", str(record.id)), (0, "active\n", ""))

        def finish(txn):
            snooze = txn.get(record.id)
            snooze.state = DONE
            snooze.answer = "french"

        Store(self.data_dir).update(finish)
        self.assertEqual(self.run_cli("status", str(record.id)), (0, "done\nfrench\n", ""))

    def test_status_unknown_or_invalid_id_exits_two(self):
        code, stdout, stderr = self.run_cli("status", "999")
        self.assertEqual((code, stdout), (2, ""))
        self.assertIn("No snooze with ID 999", stderr)

        code, stdout, stderr = self.run_cli("status", "not-an-id")
        self.assertEqual((code, stdout), (2, ""))
        self.assertIn("Invalid snooze ID", stderr)

    def test_register_all_registers_snooze_cli_command(self):
        ctx = FakeCtx()

        commands.register_all(ctx)

        self.assertIn("snooze", ctx.cli_commands)
        registration = ctx.cli_commands["snooze"]
        self.assertTrue(callable(registration["setup_fn"]))
        self.assertTrue(callable(registration["handler_fn"]))

    def test_timer_does_not_start_outside_gateway_context(self):
        with mock.patch("snooze.send.in_gateway", return_value=False):
            self.assertFalse(send.start_timer(object()))


if __name__ == "__main__":
    unittest.main()
