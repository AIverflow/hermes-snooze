import unittest

import snooze


class FakeCtx:
    """Stand-in for the Hermes plugin context."""


class RegisterTest(unittest.TestCase):
    def test_register_runs_with_fake_ctx(self):
        self.assertIsNone(snooze.register(FakeCtx()))


if __name__ == "__main__":
    unittest.main()
