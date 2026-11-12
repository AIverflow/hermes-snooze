#!/usr/bin/env python3
"""Check that the Hermes names the plugin imports, and the hooks it registers, still exist.

Run it with Hermes' own Python. Prints one `FAIL name: reason` line per name that no longer
imports and exits 1 if any failed. Standard library only.

    python scripts/hermes_compat.py
"""

import importlib
import sys

# (module, name) pairs Snooze uses from Hermes (send.py, commands.py). `name` may be dotted
# (`Class.method`), resolved with getattr. The first two are private Hermes names (send.py).
HERMES_NAMES = [
    ("tools.send_message_tool", "send_message_tool"),
    ("agent.estop", "is_engaged"),
    ("plugins.plugin_storage", "plugin_data_dir"),
    ("gateway.session_context", "get_session_env"),
    ("gateway.status", "get_running_pid"),
    ("gateway.status", "resolve_gateway_liveness"),
    ("hermes_constants", "get_hermes_home"),
    ("gateway.config", "load_gateway_config"),
    ("hermes_cli.config", "load_config_readonly"),
    ("hermes_cli.plugins", "PluginContext.current_cron_execution"),
    ("hermes_cli.plugins", "PluginContext.on_unload"),
]
# Hooks the plugin registers that older Hermes versions lack.
HERMES_HOOKS = ["post_gateway_admission"]


def lookup(module: str, name: str):
    """The object at `name` in `module`; a dotted name such as `Class.method` is walked with getattr."""
    obj = importlib.import_module(module)
    for part in name.split("."):
        obj = getattr(obj, part)
    return obj


def failed_names() -> dict:
    """{name: reason} for every Hermes name that does not import and every hook Hermes does not know."""
    failed = {}
    for module, name in HERMES_NAMES:
        try:
            lookup(module, name)
        except AttributeError:
            failed[f"{module}.{name}"] = "name not found"
        except Exception as exc:
            failed[f"{module}.{name}"] = f"{type(exc).__name__}: {exc}"
    for hook in HERMES_HOOKS:
        try:
            if hook not in importlib.import_module("hermes_cli.plugins").VALID_HOOKS:
                failed[f"hook {hook}"] = "not in hermes_cli.plugins.VALID_HOOKS"
        except Exception as exc:
            failed[f"hook {hook}"] = f"{type(exc).__name__}: {exc}"
    return failed


def main() -> int:
    failed = failed_names()
    for name, why in failed.items():
        print(f"FAIL {name}: {why}")
    if not failed:
        print("all Hermes names import")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
