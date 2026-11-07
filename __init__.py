"""snooze: a reminder that repeats until you confirm it."""


def register(ctx):
    """Plugin entry point: registers the `/snooze` command and the `snooze_create` tool.

    Glue modules (commands, hooks, send) are imported here, not at the top of the
    file, so the package loads without Hermes.
    """
    from . import commands

    commands.register_all(ctx)
