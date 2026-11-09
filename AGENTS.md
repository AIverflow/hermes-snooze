# snooze — agent guide

Hermes Agent plugin: a reminder that repeats on your phone until you confirm (word, emoji, reaction), then can hand a follow-up to the agent. Zero LLM calls per reminder.

## Language

Domain terms (snooze, reminder, occurrence, confirm rule, ...) are defined in `CONTEXT.md`. Use them in code, docs and tickets.

## Code layout

Before any ticket, read `.tickets/WORKFLOW.md`.

The repo root is the plugin folder.

| Path | What | Imports Hermes? |
|---|---|---|
| `plugin.yaml`, `__init__.py` | Manifest, `register(ctx)` | yes, but only inside `register()` |
| `commands.py`, `hooks.py`, `send.py` | Hermes glue | yes |
| `cli.py` | Hermes CLI glue | yes |
| `parse.py` | `/snooze` text → spec | **no** |
| `store.py` | `snoozes.json` + file lock | **no** |
| `echo.py` | Times, durations and the echo text shown to the user | **no** |
| `confirm.py` | ok / word / emoji / later / yes matcher | **no** |
| `schedule.py` | due / next-time rules | **no** |
| `tests/test_<module>.py` | stdlib `unittest`, one file per module | — |

- Pure modules (`parse`, `store`, `confirm`, `echo`, `schedule`) use the standard library only, and relative imports between each other (`from .parse import Spec`).
- `__init__.py` imports the glue modules inside `register()`, not at the top, so tests can load the package without Hermes.
- Tests import the plugin as package `snooze` (`from snooze import parse`). `tests/__init__.py` sets that up; don't edit it.

## Docs

`README.md` is the short user-facing overview: what Snooze does, installation, first use, and limits. Put detailed user instructions in `docs/user-guide.md` and link to that guide from the README. Avoid repeating the same instructions across docs; code and schemas are the source of truth for technical behavior, while docs explain how people use the plugin. Code comments and docstrings explain implementation, not user features.

## Tests and lint

Test and lint commands are in `docs/user-guide.md` ("Development"). Tests and Ruff must pass before a commit; the Ruff rules are in `ruff.toml`.
