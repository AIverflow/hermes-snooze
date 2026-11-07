import unittest

import snooze


class FakeCtx:
    """Stand-in for the Hermes plugin context: records what is registered."""

    def __init__(self):
        self.commands = {}
        self.tools = {}
        self.hooks = {}

    def register_hook(self, name, callback):
        self.hooks[name] = callback

    def register_command(self, name, handler, description="", args_hint="", argument_mode=None):
        self.commands[name] = handler

    def register_tool(self, name, toolset, schema, handler, **kwargs):
        self.tools[name] = handler


class RegisterTest(unittest.TestCase):
    def test_register_runs_with_fake_ctx(self):
        ctx = FakeCtx()
        self.assertIsNone(snooze.register(ctx))
        self.assertEqual(list(ctx.commands), ["snooze"])
        self.assertEqual(list(ctx.tools), ["snooze_create"])
        self.assertEqual(sorted(ctx.hooks), ["gateway_platform_event", "pre_gateway_dispatch"])


if __name__ == "__main__":
    unittest.main()
