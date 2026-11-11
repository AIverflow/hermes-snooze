"""Hermes CLI glue for creating and checking snoozes."""

from __future__ import annotations

import argparse
import sys
import time
from typing import Any

from .commands import HELP, Commands
from .parse import PLATFORMS
from .store import StoreError

POLL_SECONDS = 2


def _setup(parser: argparse.ArgumentParser) -> None:
    actions = parser.add_subparsers(dest="action", required=True)
    add_parser = actions.add_parser(
        "add",
        help="Create a snooze",
        description=HELP,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    add_parser.add_argument("text", nargs="+", help="Snooze text using the /snooze grammar")
    add_parser.add_argument("--wait", action="store_true", help="Wait for an answer")
    add_parser.add_argument(
        "--timeout", type=float, default=0, help="Stop waiting after this many seconds (0 waits forever)"
    )
    add_parser.set_defaults(_snooze_action="add")

    status_parser = actions.add_parser("status", help="Show a snooze's state and answer")
    status_parser.add_argument("id", help="Snooze ID")
    status_parser.set_defaults(_snooze_action="status")

    platform_parser = actions.add_parser("platform", help="Show or set the default platform")
    platform_parser.add_argument(
        "value", nargs="?", type=str.lower, choices=[*PLATFORMS, "source"], help="Default platform; omit to show it"
    )
    platform_parser.set_defaults(_snooze_action="platform")


def _wait(store, snooze_id: int, timeout: float, sleep_fn=time.sleep, clock=time.monotonic):
    """Wait for one occurrence to finish; return (exit code, answer)."""
    started = clock()
    while True:
        try:
            record = next((item for item in store.load() if item.id == snooze_id), None)
        except StoreError as exc:
            print(str(exc), file=sys.stderr)
            return 2, None
        if record is None:
            print(f"No snooze with ID {snooze_id}.", file=sys.stderr)
            return 2, None
        if record.state == "done":
            return 0, record.answer
        if record.state == "missed":
            return 3, None
        if record.state == "cancelled":
            return 4, None
        if timeout > 0 and clock() - started >= timeout:
            print(f"Still running: #{snooze_id}", file=sys.stderr)
            return 5, None
        sleep_fn(POLL_SECONDS)


def _handler(commands: Commands, args: argparse.Namespace) -> int:
    if args._snooze_action == "add":
        text = " ".join(args.text)
        if args.wait:
            spec = commands._parse(text)
            if getattr(spec, "days", None):
                print("--wait does not work with a recurring snooze: use status <id>.", file=sys.stderr)
                return 2
        record, message = commands.create_from_cli(text)
        if record is None:
            print(message, file=sys.stderr)
            return 2
        if args.wait:
            print(message, file=sys.stderr)
            print(f"Waiting for the answer to #{record.id}…", file=sys.stderr)
            try:
                code, answer = _wait(commands._store(), record.id, args.timeout)
            except KeyboardInterrupt:
                print(f"Stopped waiting; #{record.id} keeps running.", file=sys.stderr)
                return 130
            if code == 0:
                print(answer or "")
            return code
        print(record.id)
        print(message, file=sys.stderr)
        return 0

    if args._snooze_action == "platform":
        ok, reply = commands.platform(args.value, terminal=True)
        print(reply, file=sys.stdout if ok else sys.stderr)
        return 0 if ok else 2

    try:
        snooze_id = int(args.id)
    except (TypeError, ValueError):
        print(f"Invalid snooze ID: {args.id!r}", file=sys.stderr)
        return 2

    try:
        record = commands._store().get(snooze_id)
    except StoreError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    if record is None:
        print(f"No snooze with ID {snooze_id}.", file=sys.stderr)
        return 2

    print(record.state)
    if record.answer is not None:
        print(record.answer)
    return 0


def register(ctx: Any, commands: Commands) -> None:
    """Register the `hermes snooze` subcommand against the shared command handlers."""

    register_cli_command = getattr(ctx, "register_cli_command", None)
    if not callable(register_cli_command):
        return
    register_cli_command(
        name="snooze",
        help="Create and check Snooze reminders",
        setup_fn=_setup,
        handler_fn=lambda args: _handler(commands, args),
        description="Create a reminder from a script or check its state.",
    )
