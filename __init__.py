"""snooze: a reminder that repeats until you confirm it."""

import logging

logger = logging.getLogger(__name__)


def register(ctx):
    """Plugin entry point: registers the `/snooze` command and the `snooze_create` tool, starts the timer.

    Glue modules (commands, hooks, send) are imported here, not at the top of the
    file, so the package loads without Hermes.
    """
    from . import commands, send

    commands.register_all(ctx)
    try:
        send.start_timer(ctx)
    except Exception:  # the commands still work without the timer
        logger.warning("snooze: could not start the reminder timer", exc_info=True)
