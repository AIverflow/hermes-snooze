# snooze — agent guide

Hermes Agent plugin: a reminder that repeats on your phone until you confirm (word, emoji, reaction), then can hand a follow-up to the agent. Zero LLM calls per reminder.

## Code layout

The repo root is the plugin folder.

| Path | What | Imports Hermes? |
|---|---|---|
| `plugin.yaml`, `__init__.py` | Manifest, `register(ctx)` | yes, but only inside `register()` |
| `commands.py`, `hooks.py`, `send.py` | Hermes glue | yes |
| `parse.py` | `/snooze` text → spec | **no** |
| `store.py` | `snoozes.json` + file lock | **no** |
| `confirm.py` | ok / word / emoji / later / yes matcher | **no** |
| `schedule.py` | due / next-time rules | **no** |
| `tests/test_<module>.py` | stdlib `unittest`, one file per module | — |

- Pure modules (`parse`, `store`, `confirm`, `schedule`) use the standard library only, and relative imports between each other (`from .parse import Spec`).
- `__init__.py` imports the glue modules inside `register()`, not at the top, so tests can load the package without Hermes.
- Tests import the plugin as package `snooze` (`from snooze import parse`). `tests/__init__.py` sets that up; don't edit it.

## Tests

`python3 -m unittest discover -s tests -t .` from the repo root. Must pass before a commit.
