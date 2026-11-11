#!/usr/bin/env python3
"""Check that the Hermes names the plugin imports still exist.

Run it with Hermes' own Python. Prints one `FAIL name: reason` line per name that no longer
imports and exits 1 if any failed. Standard library only.

    python scripts/hermes_compat.py
"""

import importlib
import sys

# (module, name) pairs imported from Hermes inside send.py and commands.py.
# The first two are private Hermes names (send.py).
HERMES_NAMES = [
    ("tools.send_message_tool", "send_message_tool"),
    ("agent.estop", "is_engaged"),
    ("plugins.plugin_storage", "plugin_data_dir"),
    ("gateway.session_context", "get_session_env"),
    ("gateway.status", "get_running_pid"),
    ("gateway.status", "resolve_gateway_liveness"),
    ("hermes_constants", "get_hermes_home"),
    ("gateway.config", "load_gateway_config"),
]


def failed_names() -> dict:
    """{module.name: reason} for every Hermes name that does not import."""
    failed = {}
    for module, name in HERMES_NAMES:
        try:
            if not hasattr(importlib.import_module(module), name):
                failed[f"{module}.{name}"] = "name not found"
        except Exception as exc:
            failed[f"{module}.{name}"] = f"{type(exc).__name__}: {exc}"
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
